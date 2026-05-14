import re
import logging
import random
import time
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config.settings import (
    SERPAPI_KEY,
    LOCATOR_URL_PATTERNS,
    USER_AGENTS,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
)

logger = logging.getLogger("scraper.discovery")


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _slug_candidates(company_name: str) -> list[str]:
    """
    Generate multiple slug candidates for a company name, ordered by likelihood
    of being the domain. We can't know which one the company actually uses
    (Casey's General Stores → caseys.com; Love's Travel Stops → loves.com),
    so we try several and the URL probing picks whichever responds.
    """
    base = company_name.lower().strip()

    # Drop common trailing descriptors that aren't part of the brand domain.
    # "Love's Travel Stops & Country Stores" → "love's"
    # "Casey's General Stores" → "casey's"
    # "Wendy's Restaurants" → "wendy's"
    suffixes = [
        r"\s+(?:general\s+)?stores?$",
        r"\s+restaurants?$",
        r"\s+(?:travel\s+)?(?:stops?|center|centers|centre|plaza)$",
        r"\s+(?:&|and)\s+.*$",
        r"\s+corporation$",
        r"\s+corp\.?$",
        r"\s+company$",
        r"\s+co\.?$",
        r"\s+inc\.?$",
        r"\s+llc\.?$",
        r"\s+services$",
    ]
    trimmed = base
    for pat in suffixes:
        trimmed = re.sub(pat, "", trimmed)
    trimmed = trimmed.strip()

    def _normalize(text: str) -> list[str]:
        # Produce both "with-apostrophe-s" and "without" forms.
        # casey's → ["caseys", "casey"]
        with_s = re.sub(r"['`]", "", text)             # drop apostrophe only
        no_s   = re.sub(r"['`]s?\b", "", text)         # drop apostrophe + optional s
        out = []
        for t in (with_s, no_s):
            cleaned = re.sub(r"[^a-z0-9]+", "", t)
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return out

    candidates: list[str] = []
    for variant in (trimmed, base):
        for c in _normalize(variant):
            if c not in candidates:
                candidates.append(c)

    # Also try the first word alone (e.g. "Love's Travel Stops" → "loves")
    first_word = re.split(r"\s+", trimmed or base, maxsplit=1)[0]
    for c in _normalize(first_word):
        if c not in candidates:
            candidates.append(c)

    return candidates


def _slugify(company_name: str) -> str:
    """Backwards-compatible single-slug accessor — returns the first candidate."""
    cands = _slug_candidates(company_name)
    return cands[0] if cands else ""


def _is_likely_locator(url: str) -> bool:
    keywords = [
        "location", "store", "find", "branch", "restaurant",
        "dealer", "office", "clinic", "pharmacy", "atm",
    ]
    lower = url.lower()
    return any(kw in lower for kw in keywords)


_NON_PROD_SUBDOMAINS = {"dev", "staging", "stage", "test", "qa", "uat", "preview", "beta", "demo"}

# Treat ".co.uk", ".com.au", etc. as a single TLD so the brand label is
# what comes before them, not "co" or "com".
_MULTIPART_TLDS = {
    "co.uk", "com.au", "co.nz", "co.jp", "com.br", "com.mx", "co.za",
    "com.sg", "co.kr", "com.tr",
}

# US-eligible TLDs — we scrape US locations, so a chain's `.com` site is
# what we want, not its `.co.uk` or `.de` regional site.
_US_TLDS = {"com", "us", "org", "net", "io", "co"}
_FOREIGN_TLDS = {
    "uk", "co.uk", "au", "com.au", "ca", "nz", "co.nz", "de", "fr", "es",
    "it", "nl", "jp", "co.jp", "kr", "co.kr", "br", "com.br", "mx",
    "com.mx", "za", "co.za", "sg", "com.sg", "tr", "com.tr", "ie",
}


def _registrable_brand(hostname: str) -> str:
    """
    Return the brand label of a hostname — the segment immediately to the
    left of the TLD. Examples:
      caseys.com         -> "caseys"
      www.caseys.com     -> "caseys"
      stores.caseys.com  -> "caseys"
      caseys.ccbrands.com -> "ccbrands"  (caseys is just a subdomain here)
      example.co.uk      -> "example"
    """
    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]

    # Handle multi-part TLDs first
    for mtld in _MULTIPART_TLDS:
        if host.endswith("." + mtld):
            stripped = host[: -(len(mtld) + 1)]
            return stripped.split(".")[-1] if stripped else ""

    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return host


def _hostname_tld(hostname: str) -> str:
    """Return the TLD portion (with multi-part TLDs handled)."""
    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    for mtld in _MULTIPART_TLDS:
        if host.endswith("." + mtld):
            return mtld
    parts = host.split(".")
    return parts[-1] if parts else ""


def _hostname_matches_company(url: str, candidates: list[str]) -> bool:
    """
    Confirm the URL's hostname belongs to the company AND looks like the
    US-targeted site.

    A match is: a slug candidate matches the registrable brand label of the
    hostname (the segment just before the TLD). This prevents false positives
    like "caseys.ccbrands.com" matching "caseys" — the brand label there is
    "ccbrands", not "caseys".

    Also rejects:
      - Non-prod environments (dev.caseys.com, staging.loves.com)
      - Foreign-country sites (starbucks.co.uk when we want starbucks.com)
    """
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    parts = host.split(".")
    if parts and parts[0] in _NON_PROD_SUBDOMAINS:
        return False

    # Reject foreign-country TLDs — we want the US site
    tld = _hostname_tld(host)
    if tld in _FOREIGN_TLDS:
        return False

    brand = _registrable_brand(host)
    if not brand:
        return False

    for slug in candidates:
        if not slug:
            continue
        if slug == brand:
            return True
        # Allow "caseys" to match a brand of "caseys-corp" or similar
        # 2-character suffix tolerance, but require slug to be a prefix.
        if brand.startswith(slug) and len(brand) - len(slug) <= 2:
            return True
    return False


def _search_serpapi(company_name: str, candidates: list[str]) -> str | None:
    if not SERPAPI_KEY:
        return None
    logger.info("Trying SerpAPI for '%s'", company_name)
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={
                "q": f"{company_name} store locator",
                "api_key": SERPAPI_KEY,
                "num": 10,
            },
            headers=_get_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Prefer locator-shaped URLs on a matching domain
        for result in data.get("organic_results", []):
            link = result.get("link", "")
            if _hostname_matches_company(link, candidates) and _is_likely_locator(link):
                logger.info("SerpAPI found: %s", link)
                return link
        # Fall back to any result on a matching domain
        for result in data.get("organic_results", []):
            link = result.get("link", "")
            if _hostname_matches_company(link, candidates):
                logger.info("SerpAPI matched domain: %s", link)
                return link
    except Exception as e:
        logger.warning("SerpAPI search failed: %s", e)
    return None


def _search_duckduckgo(company_name: str, candidates: list[str]) -> str | None:
    logger.info("Trying DuckDuckGo for '%s'", company_name)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # legacy fallback
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{company_name} store locator", max_results=15))
            # Prefer a locator-shaped URL on a matching domain
            for r in results:
                href = r.get("href", "")
                if _hostname_matches_company(href, candidates) and _is_likely_locator(href):
                    logger.info("DuckDuckGo found locator: %s", href)
                    return href
            # Fall back to any result whose hostname matches the company
            for r in results:
                href = r.get("href", "")
                if _hostname_matches_company(href, candidates):
                    logger.info("DuckDuckGo matched domain: %s", href)
                    return href
            logger.info("DuckDuckGo returned %d results but none matched the company domain", len(results))
    except ImportError:
        logger.warning("ddgs not installed, skipping DDG search")
    except Exception as e:
        logger.warning("DuckDuckGo search failed: %s", e)
    return None


def _try_common_patterns(company_name: str) -> str | None:
    """
    Try common locator URL patterns against multiple slug candidates.

    For each candidate (in priority order), first check that the root domain
    exists via a HEAD request. If it does, probe locator paths with both HEAD
    and GET (some sites reject HEAD). Stop at the first 200 response.
    """
    candidates = _slug_candidates(company_name)
    logger.info("Slug candidates for '%s': %s", company_name, candidates)

    tried_roots: set[str] = set()

    # "Domain resolves" means we got *any* HTTP response — 403/503 from
    # Cloudflare still proves the domain exists (and we'll let Playwright
    # solve the challenge later). Only network errors mean "no such domain".
    def _domain_exists(host: str) -> bool:
        try:
            r = requests.head(host, headers=_get_headers(), timeout=8, allow_redirects=True)
            return True  # any HTTP response = domain resolves
        except requests.RequestException:
            return False

    for slug in candidates:
        root_url = f"https://www.{slug}.com"
        if root_url in tried_roots:
            continue
        tried_roots.add(root_url)
        if not (_domain_exists(root_url) or _domain_exists(f"https://{slug}.com")):
            logger.debug("Domain '%s' unreachable", slug)
            continue

        logger.info("Domain '%s' resolves — probing locator paths", slug)

        # Probe locator URL patterns for this slug. Only accept clean 200
        # responses. Short timeouts prevent slow servers (or bot-blocked
        # connections that hang) from dragging out discovery.
        for pattern in LOCATOR_URL_PATTERNS:
            url = pattern.format(slug=slug)
            try:
                resp = requests.head(
                    url, headers=_get_headers(), timeout=4, allow_redirects=True,
                )
                if resp.status_code == 200:
                    logger.info("Common pattern found: %s", url)
                    return url
                if resp.status_code == 405:
                    # HEAD not allowed — try GET
                    g = requests.get(url, headers=_get_headers(), timeout=5, allow_redirects=True)
                    if g.status_code == 200:
                        logger.info("Common pattern found (GET): %s", url)
                        return url
            except requests.RequestException:
                continue
            time.sleep(random.uniform(0.2, 0.5))

    logger.info("No common URL patterns matched for '%s'", company_name)
    return None


def _crawl_main_site_for_locator(company_name: str, candidates: list[str]) -> str | None:
    """
    Find the company's main site (via direct domain probe or search), then
    scan its HTML for links containing locator-ish keywords.
    """
    logger.info("Trying main-site crawl for '%s'", company_name)

    # First: try the candidate slugs as direct domains. This is more reliable
    # than search and avoids any cross-company false matches.
    main_url: str | None = None
    for slug in candidates:
        for root in (f"https://www.{slug}.com", f"https://{slug}.com"):
            try:
                r = requests.head(root, headers=_get_headers(), timeout=8, allow_redirects=True)
                if r.status_code < 400:
                    main_url = str(r.url) if r.url else root
                    logger.info("Found main site via direct probe: %s", main_url)
                    break
            except requests.RequestException:
                continue
        if main_url:
            break

    # Otherwise, fall back to DDG to find the official site, with a domain check.
    if not main_url:
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(f"{company_name} official site", max_results=5))
                for r in results:
                    href = r.get("href", "")
                    if href.startswith("http") and _hostname_matches_company(href, candidates):
                        main_url = href
                        logger.info("Found main site via DDG: %s", main_url)
                        break
        except Exception as e:
            logger.warning("DDG site search failed: %s", e)

    if not main_url:
        return None

    try:
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        main_resp = requests.get(main_url, headers=_get_headers(), timeout=15)
        main_resp.raise_for_status()
        main_soup = BeautifulSoup(main_resp.text, "lxml")

        # Score candidate links — prefer ones whose text explicitly says
        # "find a store", "locations", etc. over generic "stores" footer links.
        best: tuple[int, str] | None = None
        scoring_phrases = [
            ("find a store", 10), ("find a location", 10), ("store locator", 10),
            ("find us", 8), ("locations", 6), ("our stores", 6), ("our locations", 7),
            ("store finder", 9), ("find a restaurant", 10), ("restaurants near you", 9),
            ("dealer locator", 10), ("branch locator", 10),
        ]

        for a_tag in main_soup.find_all("a", href=True):
            link_href = a_tag["href"]
            link_text = a_tag.get_text(strip=True).lower()
            combined = f"{link_href.lower()} {link_text}"
            score = 0
            for phrase, pts in scoring_phrases:
                if phrase in combined:
                    score = max(score, pts)
            if score == 0 and any(kw in combined for kw in ["location", "store-locator", "find-a-store"]):
                score = 3
            if score > 0:
                full_url = urljoin(main_url, link_href)
                if best is None or score > best[0]:
                    best = (score, full_url)

        if best:
            logger.info("Found locator link (score %d): %s", best[0], best[1])
            return best[1]
    except Exception as e:
        logger.warning("Main-site crawl failed: %s", e)
    return None


def discover_locator_url(company_name: str) -> tuple[str | None, str]:
    """
    Discover the store locator URL for a company.

    Order of strategies (most reliable first):
      1. Common URL patterns on resolved candidate domains — deterministic,
         no false positives, no rate limits.
      2. Main-site crawl — find the official domain and scan its homepage
         for "Find a store / Locations / Store locator" links.
      3. SerpAPI (if configured) — verified against the candidate domains.
      4. DuckDuckGo — verified against the candidate domains.

    Returns (url, method_used).
    """
    candidates = _slug_candidates(company_name)

    # Strategy 1: Common URL patterns on resolved candidate domains
    url = _try_common_patterns(company_name)
    if url:
        return url, "common_pattern"

    # Strategy 2: Main-site crawl
    url = _crawl_main_site_for_locator(company_name, candidates)
    if url:
        return url, "site_crawl"

    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    # Strategy 3: SerpAPI
    url = _search_serpapi(company_name, candidates)
    if url:
        return url, "serpapi"

    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    # Strategy 4: DuckDuckGo (with domain verification)
    url = _search_duckduckgo(company_name, candidates)
    if url:
        return url, "duckduckgo"

    logger.warning("Could not discover locator URL for '%s'", company_name)
    return None, "none"
