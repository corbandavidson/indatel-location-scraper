"""
LLM planner — uses Gemini Flash (or another configured model) to make the
two decisions the legacy auto-discovery is bad at:

  1. What's the actual store-locator URL for this company?
  2. Given the rendered page, which extraction strategy fits it?

Each call is cheap (sub-cent on Gemini Flash, free at low volume on the
google AI Studio tier). If no API key is configured the planner becomes
inert and the orchestrator falls back to the legacy code paths.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("scraper_ai.planner")


STRATEGY_NAMES = (
    "jsonld",          # Top-level JSON-LD on the locator page
    "embedded_json",   # window.__NEXT_DATA__-style blobs
    "api_sweep",       # Search-based locator — sweep zip codes
    "directory_tree",  # Yext/Uberall state→city→store directory
    "sitemap",         # Sitemap of individual store pages
    "llm_extract",     # Last resort: ask the LLM to read the HTML
)


@dataclass
class PlannerConfig:
    api_key: str
    model: str = "gemini-2.5-flash"   # free tier, current stable Flash
    timeout_seconds: int = 30


# Tried in order if the primary model isn't reachable. Lets us tolerate
# Google retiring older model IDs without releasing a new app build.
FALLBACK_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)


def _excerpt_html(html: str, limit: int = 18_000) -> str:
    """
    Trim HTML to a planner-sized excerpt. Strip <script> and <style> bodies
    since they balloon the size with non-structural content.
    """
    if not html:
        return ""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    if len(cleaned) > limit:
        # Keep the head + the start of the body — that's where the page
        # structure and links live.
        cleaned = cleaned[:limit] + "\n<!-- truncated -->"
    return cleaned


class Planner:
    """
    A thin wrapper around google-genai. Every method returns None on failure
    so callers can transparently fall back to legacy logic.
    """

    def __init__(self, config: PlannerConfig):
        self.config = config
        self._client = None

    def _client_lazy(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai
            self._client = genai.Client(api_key=self.config.api_key)
            return self._client
        except Exception as e:
            logger.warning("Failed to init AI client: %s", e)
            return None

    def _generate(self, prompt: str) -> Optional[str]:
        client = self._client_lazy()
        if client is None:
            return None

        # Try configured model first, then walk the fallback list. If the
        # configured one returns 404 (deprecated), remember the successful
        # model so subsequent calls skip the retry loop.
        models_to_try = [self.config.model] + [
            m for m in FALLBACK_MODELS if m != self.config.model
        ]
        last_error = None
        for model_id in models_to_try:
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                )
                # Remember a working model for the rest of this run
                if model_id != self.config.model:
                    logger.info("AI: switched to fallback model (primary unavailable)")
                    _ = model_id  # internal detail, don't leak to user-facing logs
                    self.config.model = model_id
                return getattr(resp, "text", None) or ""
            except Exception as e:
                last_error = e
                # Only retry the next model if this looked like a "model not
                # found" error. Other errors (rate limit, auth, network) should
                # fail fast.
                msg = str(e).lower()
                if "404" in msg or "not found" in msg or "is not supported" in msg:
                    continue
                logger.warning("AI call failed: %s", e)
                return None
        logger.warning("AI unavailable; last error: %s", last_error)
        return None

    # ── Public API ───────────────────────────────────────────────────

    def find_locator_url(self, company_name: str) -> Optional[str]:
        """
        Ask the LLM where this company's US store locator lives. Returns
        a URL string or None.
        """
        prompt = (
            "You are picking the URL of a US retail chain's full store "
            "DIRECTORY — a page that LISTS EVERY physical store location "
            "(typically state-by-state), NOT a search widget that only "
            "shows stores 'near me' based on the visitor's location.\n\n"
            f"Chain: '{company_name}'\n\n"
            "This distinction is critical. A page like '/store-locator' "
            "usually contains a map + search box and pre-loads only ~10 "
            "stores nearby — we get junk results from those. A page like "
            "'/store-directory' contains links to all 50 states and then "
            "to every store — that's what we want.\n\n"
            "STRONG preferences, in order:\n"
            "  1. A dedicated subdomain like 'stores.{brand}.com' or "
            "'locations.{brand}.com' — these are Yext/Uberall-hosted "
            "directory trees and are by far the best target.\n"
            "  2. A directory path: '/store-directory', '/stores' "
            "(plural), '/locations/all', '/our-stores', '/store-list', "
            "'/all-stores'.\n"
            "  3. As a last resort, a generic '/locations' path.\n\n"
            "AVOID — these are geo-aware widgets that only return "
            "nearby stores:\n"
            "  - '/store-locator' (singular)\n"
            "  - '/find-a-store', '/find-store', '/store-finder'\n"
            "  - any path ending in '-locator', '-finder', or '/near-me'\n"
            "  - E-commerce paths like 'shop.{brand}.com/...'\n"
            "  - Country-specific TLDs like .co.uk, .com.au, .de — we want "
            "the US site.\n"
            "  - URLs you are not confident actually exist.\n\n"
            "Good examples:\n"
            "  - https://stores.advanceautoparts.com/\n"
            "  - https://www.dollargeneral.com/store-directory\n"
            "  - https://locations.starbucks.com/\n"
            "  - https://www.walgreens.com/storelistings/storesbystate.jsp\n\n"
            "Return ONLY the URL on one line, nothing else. No markdown, "
            "no quotes, no explanation. If you don't know the chain or "
            "it's not US-based, reply with the single word: NONE"
        )
        result = self._generate(prompt)
        if not result:
            return None
        text = result.strip().splitlines()[0].strip()
        if text.upper() == "NONE":
            return None
        m = re.search(r"https?://[^\s'\"<>]+", text)
        return m.group(0).rstrip(".,") if m else None

    def pick_strategy(self, html: str, page_url: str) -> Optional[str]:
        """
        Given the rendered locator page, return the best extraction strategy
        name (one of STRATEGY_NAMES) or None.
        """
        excerpt = _excerpt_html(html)
        prompt = (
            "You are a web scraping strategist. Decide which extraction "
            "strategy fits this page best. Return ONE of these exact words:\n"
            f"  {', '.join(STRATEGY_NAMES)}\n\n"
            "Definitions:\n"
            "  jsonld         — page has <script type=application/ld+json> "
            "with store/LocalBusiness records embedded directly.\n"
            "  embedded_json  — page has a large JS object (e.g. "
            "window.__NEXT_DATA__, __INITIAL_STATE__) containing store data.\n"
            "  api_sweep      — page is a search form / map widget. Stores "
            "are fetched via an internal API based on a zip or lat/lng.\n"
            "  directory_tree — page links out to state pages, which link to "
            "city pages, which link to individual store pages.\n"
            "  sitemap        — page itself has no store data, but a "
            "sitemap.xml likely lists every store page.\n"
            "  llm_extract    — page has store data in plain HTML cards "
            "that don't match any of the above.\n\n"
            f"Page URL: {page_url}\n\n"
            "Page HTML excerpt:\n"
            "----- BEGIN HTML -----\n"
            f"{excerpt}\n"
            "----- END HTML -----\n\n"
            "Reply with just the strategy name."
        )
        result = self._generate(prompt)
        if not result:
            return None
        token = result.strip().split()[0].strip().lower().strip(".,'\"")
        if token in STRATEGY_NAMES:
            return token
        # Tolerant matching
        for s in STRATEGY_NAMES:
            if s in result.lower():
                return s
        return None

    def estimate_store_count(self, company_name: str) -> Optional[dict]:
        """
        Ask the LLM for a rough estimate of how many US stores this chain
        has, and whether it's a national or regional operator. Used as
        the threshold for post-extraction validation — if we got 50 stores
        but the chain has ~20K, something went wrong.

        Returns a dict like {"count": 20000, "is_national": True, "regions": []}
        or None if the model didn't recognize the chain.
        """
        prompt = (
            "Roughly how many physical US retail locations does this chain "
            "operate? Also indicate whether they're nationwide (all 50 "
            "states) or regional.\n\n"
            f"Chain: '{company_name}'\n\n"
            "Reply with ONLY this JSON shape, no markdown fences, no prose:\n"
            '  {"count": <integer>, "is_national": <true|false>, "regions": [<2-letter state codes if regional>]}\n\n'
            "Examples:\n"
            '  Dollar General: {"count": 20000, "is_national": true, "regions": []}\n'
            '  Walgreens: {"count": 8500, "is_national": true, "regions": []}\n'
            '  HEB: {"count": 380, "is_national": false, "regions": ["TX","NM"]}\n'
            '  Wawa: {"count": 1000, "is_national": false, "regions": ["NJ","PA","DE","MD","VA","FL","DC"]}\n\n'
            'If you do not recognize the chain, reply with: {"count": 0, "is_national": false, "regions": []}'
        )
        result = self._generate(prompt)
        if not result:
            return None
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("count", 0) <= 0:
            return None
        return {
            "count": int(data.get("count", 0)),
            "is_national": bool(data.get("is_national", False)),
            "regions": [r.upper() for r in data.get("regions", []) if isinstance(r, str)],
        }

    def find_better_locator_url(
        self,
        company_name: str,
        previous_url: str,
        previous_count: int,
        reason: str,
    ) -> Optional[str]:
        """
        After validation flags the first scrape as suspect, ask the LLM
        for a DIFFERENT URL that's more likely to contain the full store
        directory. Pass it the previous URL and how it failed so it
        doesn't just suggest the same thing again.
        """
        prompt = (
            f"You picked '{previous_url}' as the store directory for "
            f"'{company_name}', but it only produced {previous_count} "
            f"locations ({reason}). That URL was probably a geo-aware "
            "'near me' widget rather than a full directory.\n\n"
            "Suggest a DIFFERENT URL for the same chain that's more likely "
            "to list every US store. Strong candidates:\n"
            "  - https://stores.{brand}.com/ or https://locations.{brand}.com/\n"
            f"  - https://www.{{brand}}.com/store-directory\n"
            f"  - https://www.{{brand}}.com/stores  (plural — full list)\n"
            f"  - https://www.{{brand}}.com/locations/all\n"
            "  - A sitemap path like https://www.{brand}.com/sitemap.xml\n\n"
            "Do NOT return the same URL you returned before. Do NOT return "
            "any '-locator' or '-finder' style URL.\n\n"
            "Return ONLY the URL on one line, no markdown, no quotes. "
            "If you cannot think of a better one, reply: NONE"
        )
        result = self._generate(prompt)
        if not result:
            return None
        text = result.strip().splitlines()[0].strip()
        if text.upper() == "NONE":
            return None
        m = re.search(r"https?://[^\s'\"<>]+", text)
        if not m:
            return None
        url = m.group(0).rstrip(".,")
        # Defensive: don't loop back to the same URL
        if url.rstrip("/") == previous_url.rstrip("/"):
            return None
        return url

    def extract_stores_from_html(self, html: str, page_url: str) -> list[dict]:
        """
        Last-resort: feed the HTML to the LLM and ask for store records as
        JSON. Returns a list of dicts (possibly empty).
        """
        excerpt = _excerpt_html(html, limit=60_000)
        prompt = (
            "Extract every distinct retail store location from this HTML. "
            "Return a JSON array. Each element must have these keys exactly:\n"
            '  street_address, city, state, zip_code, phone_number, location_name\n'
            "Use empty strings for missing fields. Skip duplicates. Skip "
            "non-store content (career pages, recipes, etc.). Return ONLY the "
            "JSON array — no markdown fences, no commentary.\n\n"
            f"Page URL: {page_url}\n\n"
            "HTML:\n"
            "----- BEGIN HTML -----\n"
            f"{excerpt}\n"
            "----- END HTML -----"
        )
        result = self._generate(prompt)
        if not result:
            return []
        # Strip code fences if the model added them
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("LLM returned non-JSON: %s", e)
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def get_planner_from_env() -> Optional[Planner]:
    """
    Build a Planner from environment variables. Returns None if no API key
    is configured (lets the caller silently fall back to legacy code).
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
    return Planner(PlannerConfig(api_key=api_key, model=model))
