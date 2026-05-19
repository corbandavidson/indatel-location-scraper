"""
LLM-orchestrated scrape pipeline.

The planner picks the URL and the extraction strategy; the existing scraper
primitives (in the unmodified ../scraper package) do the heavy lifting.
If the planner is disabled (no API key), we transparently fall back to the
legacy auto-discovery + try-all-strategies flow.
"""

import logging
import random
import sys
import time
from pathlib import Path

# Reach into the unmodified parent scraper package without changing it.
_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import requests

from config.settings import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, MAX_LOCATIONS, USER_AGENTS
from scraper.discovery import discover_locator_url, _slug_candidates
from scraper.renderer import render_page
from scraper.extractor import (
    extract_jsonld,
    extract_embedded_json,
    extract_from_apis,
    extract_html_locations,
    extract_from_directory_tree,
    extract_from_sitemap,
    probe_direct_apis,
)
from scraper.cleaner import clean_locations
from scraper.extractor import _try_json_get, _extract_from_json, _US_STATE_CODES

from scraper_ai.planner import Planner
from scraper_ai.stealth import render_stealth

logger = logging.getLogger("scraper_ai.orchestrator")


# Minimum location count below which we treat a strategy result as suspect
# and try a different approach.
_MIN_PLAUSIBLE = 5

# Validation thresholds for the post-extraction sanity check.
# Without an expected count we use conservative heuristics — the goal is to
# catch obvious "only nearby stores" results without flagging legitimate
# small regional chains.
_MIN_LOCATIONS_NO_EXPECT = 5
_MIN_STATES_FOR_NATIONAL = 10
_FRACTION_OF_EXPECTED = 0.20   # got <20% of expected → suspicious

# Pages smaller than this after rendering are almost always Cloudflare
# challenge pages, redirects, or empty wrappers — not the real locator.
_MIN_USEFUL_HTML_BYTES = 1500


def _render_is_blocked(result) -> bool:
    """True if a render produced something too small/invalid to extract from."""
    if result is None:
        return True
    final = (result.final_url or "")
    # Playwright leaves final_url at chrome-error:// when navigation fails
    # (HTTP/2 protocol errors, DNS failures, etc.). Such pages are unusable.
    if final.startswith(("chrome-error://", "about:", "data:")):
        return True
    html = (result.html or "")
    if len(html) < _MIN_USEFUL_HTML_BYTES:
        return True
    # Cloudflare interstitial signature
    if "Just a moment" in html[:600] or "challenge-platform" in html[:1500]:
        return True
    return False


def _alt_directory_urls(company_name: str) -> list[str]:
    """Common Yext/Uberall directory subdomain patterns to try when the
    LLM-chosen URL turns out to be junk."""
    out: list[str] = []
    for slug in _slug_candidates(company_name):
        for tmpl in (
            "https://stores.{slug}.com/index.html",
            "https://stores.{slug}.com/",
            "https://locations.{slug}.com/index.html",
            "https://locations.{slug}.com/",
            "https://www.{slug}.com/stores",
            "https://www.{slug}.com/store-directory",
            "https://www.{slug}.com/store-locator",
            "https://www.{slug}.com/locations/all-locations",
            "https://m.{slug}.com/locations/all-locations",
            "https://m.{slug}.com/locations",
        ):
            url = tmpl.format(slug=slug)
            if url not in out:
                out.append(url)
    return out


def _try_alt_urls(company_name: str, planner: Planner | None, step):
    """
    When the planner's URL fails, walk through common subdomain patterns
    looking for one that returns a real page. Returns (render_result, url)
    or (None, None).
    """
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    for alt in _alt_directory_urls(company_name):
        step("rendering", f"fallback: {alt}")
        try:
            r = requests.get(alt, headers=headers, timeout=10, allow_redirects=True)
        except requests.RequestException:
            continue
        if r.status_code != 200 or len(r.text) < _MIN_USEFUL_HTML_BYTES:
            continue
        # We got real HTML — render through legacy first, then stealth
        # as a fallback if legacy gets blocked.
        result = render_page(str(r.url))
        if _render_is_blocked(result):
            stealth_result = render_stealth(str(r.url))
            if stealth_result and not _render_is_blocked(stealth_result):
                result = stealth_result
            else:
                continue
        logger.info("[%s] Alt URL succeeded: %s (%d bytes)",
                    company_name, r.url, len(result.html))
        return result, str(r.url)
    return None, None


def _try_ai_api_suggestions(
    company_name: str, failed_url: str, reason: str,
    planner: Planner, step,
) -> list[dict]:
    """
    Ask the AI planner to suggest direct API endpoints, then try each one.
    Handles {state} placeholders by iterating all 50 US states.
    """
    step("extracting", "AI suggesting API endpoints")
    suggestions = planner.suggest_api_endpoints(company_name, failed_url, reason)
    if not suggestions:
        return []

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*",
    }

    for api_url in suggestions:
        logger.info("[%s] Trying AI-suggested endpoint: %s", company_name, api_url)

        if "{state}" in api_url:
            step("extracting", f"AI endpoint: sweeping all states")
            all_locs: list[dict] = []
            for st in _US_STATE_CODES:
                concrete = api_url.replace("{state}", st)
                time.sleep(random.uniform(0.3, 0.8))
                data = _try_json_get(concrete, headers)
                if data is not None:
                    all_locs.extend(_extract_from_json(data, concrete))
            if all_locs:
                logger.info("[%s] AI state-sweep found %d locations", company_name, len(all_locs))
                return all_locs
            continue

        step("extracting", f"AI endpoint: {api_url}")
        time.sleep(random.uniform(0.5, 1.5))

        # Try as JSON API first
        data = _try_json_get(api_url, headers)
        if data is not None:
            locs = _extract_from_json(data, api_url)
            if locs:
                logger.info("[%s] AI endpoint found %d locations at %s",
                            company_name, len(locs), api_url)
                return locs

        # Try rendering if it looks like an HTML URL
        try:
            r = requests.get(api_url, headers={"User-Agent": random.choice(USER_AGENTS)},
                             timeout=10, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > _MIN_USEFUL_HTML_BYTES:
                result = render_page(str(r.url))
                if result and not _render_is_blocked(result):
                    from scraper.extractor import extract_locations
                    locs = extract_locations(result, company_name)
                    if locs:
                        logger.info("[%s] AI URL rendered with %d locations: %s",
                                    company_name, len(locs), api_url)
                        return locs
        except requests.RequestException:
            pass

    return []


def _validate_locations(cleaned: list[dict], expected: dict | None) -> tuple[bool, str]:
    """
    Decide whether a cleaned result set is plausible or whether we got
    bitten by a geo-aware locator that only returned nearby stores.

    Returns (is_valid, reason_if_invalid). Reason is a short human-readable
    string suitable for both logs and the retry prompt.
    """
    n = len(cleaned)
    if n == 0:
        return False, "no locations extracted"

    states = {
        (loc.get("state") or "").strip().upper()
        for loc in cleaned
    }
    states.discard("")
    n_states = len(states)

    if expected:
        exp_count = expected.get("count", 0)
        is_national = expected.get("is_national", False)
        # Hard-fail: less than 20% of expected for any non-tiny chain
        if exp_count >= 50 and n < exp_count * _FRACTION_OF_EXPECTED:
            return False, f"got {n} locations, expected ~{exp_count}"
        # National chain bottled up in too few states is a classic geo-filter symptom
        if is_national and exp_count > 200 and n_states < _MIN_STATES_FOR_NATIONAL:
            return False, f"national chain but only {n_states} state(s) represented"
        # Regional chain that exceeded its declared regions is fine —
        # we don't penalize over-collection.
        return True, ""

    # No expected count from the AI — fall back to conservative heuristics
    if n < _MIN_LOCATIONS_NO_EXPECT:
        return False, f"only {n} location(s) extracted"
    # All-in-one-state with a small count is almost always a geo-filter
    if n_states == 1 and n < 50:
        return False, f"all {n} locations are in a single state"

    return True, ""


def _run_strategy(name: str, render_result, company: str, planner: Planner | None,
                  dir_progress=None) -> list[dict]:
    """Execute one named extraction strategy and return raw locations."""
    html = render_result.html
    url = render_result.final_url

    url_is_real = url and not url.startswith(("chrome-error://", "about:", "data:"))

    if name == "jsonld":
        return extract_jsonld(html, url)
    if name == "embedded_json":
        return extract_embedded_json(html, url)
    if name == "api_sweep":
        return extract_from_apis(render_result.intercepted_apis, url) if render_result.intercepted_apis else []
    if name == "directory_tree":
        if not url_is_real:
            return []
        return extract_from_directory_tree(html, url, progress_cb=dir_progress)
    if name == "sitemap":
        if not url_is_real:
            return []
        return extract_from_sitemap(url)
    if name == "llm_extract":
        if planner is None or not html:
            return []
        return planner.extract_stores_from_html(html, url)
    return []


def _try_all_strategies(render_result, company: str, planner: Planner | None,
                        dir_progress=None) -> list[dict]:
    """
    Run every cheap strategy in turn — JSON-LD, embedded JSON, API sniff,
    HTML cards, directory tree, sitemap — and return the first non-empty
    result. This is what the legacy scraper does too.
    """
    html = render_result.html
    url = render_result.final_url
    url_is_real = url and not url.startswith(("chrome-error://", "about:", "data:"))

    locations = extract_jsonld(html, url)
    if locations:
        logger.info("[%s] JSON-LD: %d", company, len(locations))
        return locations

    locations = extract_embedded_json(html, url)
    if locations:
        logger.info("[%s] Embedded JSON: %d", company, len(locations))
        return locations

    if render_result.intercepted_apis:
        locations = extract_from_apis(render_result.intercepted_apis, url)
        if locations:
            logger.info("[%s] API sniff: %d", company, len(locations))
            return locations

    if url_is_real:
        locations = extract_from_directory_tree(html, url, progress_cb=dir_progress)
        if locations:
            logger.info("[%s] Directory tree: %d", company, len(locations))
            return locations

    locations = extract_html_locations(html, url)
    if locations:
        logger.info("[%s] HTML parse: %d", company, len(locations))
        return locations

    if url_is_real:
        locations = extract_from_sitemap(url)
        if locations:
            logger.info("[%s] Sitemap: %d", company, len(locations))
            return locations

    return []


def scrape_company_ai(
    company_name: str,
    planner: Planner | None,
    manual_url: str | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Scrape one company. If a planner is provided, it gets first pick at
    URL and strategy. Otherwise we use the legacy pipeline as-is.
    """

    def step(label, detail=""):
        if progress_callback:
            try:
                progress_callback(label, company_name, detail)
            except Exception:
                pass

    # ── Step 1: pick the URL ─────────────────────────────────────────
    url = (manual_url or "").strip() or None
    method = "manual" if url else None

    if url is None and planner is not None:
        step("discovery", "AI finding locator URL")
        url = planner.find_locator_url(company_name)
        if url:
            method = "ai"
            logger.info("[%s] LLM proposed URL: %s", company_name, url)

    if url is None:
        step("discovery", "running legacy discovery")
        url, method = discover_locator_url(company_name)

    if not url:
        logger.warning("[%s] Could not find a locator URL", company_name)
        return []

    logger.info("[%s] Using URL (%s): %s", company_name, method, url)

    def _dir_progress(phase, current, total):
        if not total:
            return
        pct = int(current / total * 100)
        labels = {"estimate": "estimated total", "states": "states",
                  "cities": "cities", "stores": "stores"}
        step("extracting", f"crawling {labels.get(phase, phase)}: {current:,}/{total:,} ({pct}%)")

    # ── Step 2: render ───────────────────────────────────────────────
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
    step("rendering", url)
    result = render_page(url)
    if not result:
        logger.warning("[%s] Failed to render %s", company_name, url)
    else:
        logger.info("[%s] Rendered via %s (%d bytes)",
                    company_name, result.method, len(result.html))

    # If the chosen URL returned a Cloudflare challenge / empty wrapper,
    # try harder: first with stealth-patched Playwright (defeats most
    # Bot Management deployments), then walk common subdomain patterns.
    if _render_is_blocked(result):
        logger.info("[%s] Page looks blocked — retrying with stealth", company_name)
        step("rendering", "stealth retry (anti-bot)")
        stealth_result = render_stealth(url)
        if stealth_result and not _render_is_blocked(stealth_result):
            logger.info("[%s] Stealth render succeeded (%d bytes, %d APIs)",
                        company_name, len(stealth_result.html),
                        len(stealth_result.intercepted_apis))
            result = stealth_result
            method = (method + "+stealth") if method else "stealth"
        else:
            # Stealth also blocked — fall through to subdomain probe
            logger.info("[%s] Stealth also blocked — trying alt URLs", company_name)
            alt_result, alt_url = _try_alt_urls(company_name, planner, step)
            if alt_result is not None:
                result = alt_result
                url = alt_url
                method = (method + "+alt") if method else "alt"

    if result is None or _render_is_blocked(result):
        # Every render path failed — ask the AI for direct API endpoints
        # we can hit without a browser, then fall back to the dumb probe.
        logger.warning("[%s] No usable render — trying AI-suggested API endpoints", company_name)
        locations: list[dict] = []
        if planner is not None:
            locations = _try_ai_api_suggestions(
                company_name, url, "all rendering blocked by anti-bot", planner, step,
            )
        if not locations:
            step("extracting", "probing common API paths")
            locations = probe_direct_apis(url, company_name)
        if not locations:
            logger.warning("[%s] No locations extracted", company_name)
            return []
        step("cleaning", "")
        cleaned = clean_locations(locations, company_name)
        if MAX_LOCATIONS > 0 and len(cleaned) > MAX_LOCATIONS:
            cleaned = cleaned[:MAX_LOCATIONS]
        logger.info("[%s] Final: %d locations (from API probe)", company_name, len(cleaned))
        return cleaned

    # ── Step 3: pick + run strategy ──────────────────────────────────
    locations: list[dict] = []

    if planner is not None:
        step("extracting", "AI picking extraction strategy")
        strategy = planner.pick_strategy(result.html, result.final_url)
        if strategy:
            logger.info("[%s] LLM picked strategy: %s", company_name, strategy)
            step("extracting", f"strategy: {strategy}")
            locations = _run_strategy(strategy, result, company_name, planner,
                                     dir_progress=_dir_progress)
            logger.info("[%s] %s yielded %d raw", company_name, strategy, len(locations))

    # ── Step 4: legacy multi-strategy as safety net ──────────────────
    if len(locations) < _MIN_PLAUSIBLE:
        step("extracting", "running legacy multi-strategy")
        legacy = _try_all_strategies(result, company_name, planner,
                                     dir_progress=_dir_progress)
        if len(legacy) > len(locations):
            locations = legacy

    # ── Step 5: Playwright retry if static found nothing ─────────────
    if not locations and result.method == "static":
        step("rendering", "retrying with Playwright")
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        pw = render_page(url, force_playwright=True)
        if pw:
            locations = _try_all_strategies(pw, company_name, planner,
                                             dir_progress=_dir_progress)
            result = pw

    # ── Step 6: LLM last-resort extraction ───────────────────────────
    if not locations and planner is not None:
        step("extracting", "AI reading HTML directly")
        locations = planner.extract_stores_from_html(result.html, result.final_url)
        if locations:
            logger.info("[%s] LLM extract: %d", company_name, len(locations))

    # ── Step 7: AI-suggested API endpoints ───────────────────────────
    if not locations and planner is not None:
        locations = _try_ai_api_suggestions(
            company_name, url, "no extraction strategy found data", planner, step,
        )

    # ── Step 8: direct API probe (hardcoded patterns as last resort) ─
    if not locations:
        step("extracting", "probing common API paths")
        locations = probe_direct_apis(url, company_name)

    if not locations:
        logger.warning("[%s] No locations extracted", company_name)
        return []

    # ── Step 8: clean ────────────────────────────────────────────────
    step("cleaning", "")
    cleaned = clean_locations(locations, company_name)

    # ── Step 9: validate + retry once with a different URL ──────────
    # If results look like a geo-aware "near me" widget (too few stores,
    # all in one state, etc.), ask the AI for an alternate URL and try
    # again. Keep whichever attempt yielded more locations.
    expected = planner.estimate_store_count(company_name) if planner is not None else None
    if expected:
        logger.info("[%s] Expected ~%d stores (national=%s)",
                    company_name, expected["count"], expected["is_national"])
    is_valid, reason = _validate_locations(cleaned, expected)

    if not is_valid and planner is not None:
        logger.warning("[%s] Validation failed: %s — trying alternate URL",
                       company_name, reason)
        step("validating", f"results look incomplete ({reason})")
        alt_url = planner.find_better_locator_url(
            company_name, url, len(cleaned), reason,
        )
        if alt_url:
            logger.info("[%s] AI suggested alternate URL: %s", company_name, alt_url)
            step("rendering", f"retry: {alt_url}")
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            retry_result = render_page(alt_url)
            if _render_is_blocked(retry_result):
                retry_result = render_stealth(alt_url)
            if retry_result is not None and not _render_is_blocked(retry_result):
                step("extracting", "retry: multi-strategy")
                retry_locs = _try_all_strategies(retry_result, company_name, planner,
                                                 dir_progress=_dir_progress)
                if not retry_locs and planner is not None:
                    retry_locs = planner.extract_stores_from_html(
                        retry_result.html, retry_result.final_url,
                    )
                retry_cleaned = clean_locations(retry_locs, company_name)
                # Take the bigger set — never let retry shrink our result
                if len(retry_cleaned) > len(cleaned):
                    logger.info("[%s] Retry improved: %d → %d locations",
                                company_name, len(cleaned), len(retry_cleaned))
                    cleaned = retry_cleaned
                else:
                    logger.info("[%s] Retry not better (%d vs %d), keeping original",
                                company_name, len(retry_cleaned), len(cleaned))
            else:
                logger.info("[%s] Retry URL also blocked/empty", company_name)
        else:
            logger.info("[%s] AI could not suggest a better URL", company_name)

    # ── Step 10: cap + return ────────────────────────────────────────
    if MAX_LOCATIONS > 0 and len(cleaned) > MAX_LOCATIONS:
        cleaned = cleaned[:MAX_LOCATIONS]
    logger.info("[%s] Final: %d locations", company_name, len(cleaned))
    return cleaned
