import re
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config.settings import (
    USER_AGENTS,
    JS_RENDER_MARKERS,
    API_ENDPOINT_PATTERNS,
    VIEWPORT,
    BROWSER,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
)

logger = logging.getLogger("scraper.renderer")


@dataclass
class RenderResult:
    html: str
    final_url: str
    method: str  # "static" or "playwright"
    intercepted_apis: list[dict] = field(default_factory=list)
    status_code: int = 200


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }


def _looks_js_rendered(html: str) -> bool:
    if len(html.strip()) < 2000:
        return True

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(strip=True)

    # Very little visible text = definitely JS-rendered shell
    if len(text) < 500:
        return True

    address_patterns = [
        r"\d{1,5}\s+\w+\s+(st|street|ave|avenue|blvd|rd|road|dr|drive|ln|lane|way|ct|court)",
        r"\b\d{5}(-\d{4})?\b",
        r"\(\d{3}\)\s*\d{3}[-.]?\d{4}",
    ]
    has_address_content = any(
        re.search(p, text, re.IGNORECASE) for p in address_patterns
    )

    extended_markers = JS_RENDER_MARKERS + [
        "bundleJs", "chunk", "webpack", "_buildManifest",
        "self.__next", "webpackChunk",
    ]
    has_js_markers = any(marker in html for marker in extended_markers)

    if has_js_markers and not has_address_content:
        return True

    return False


def _is_api_endpoint(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in API_ENDPOINT_PATTERNS)


def _try_locator_search(page):
    """Try to enter a broad search query in a store locator search box."""
    search_selectors = [
        'input[placeholder*="zip" i]',
        'input[placeholder*="city" i]',
        'input[placeholder*="address" i]',
        'input[placeholder*="location" i]',
        'input[placeholder*="search" i]',
        'input[placeholder*="enter" i]',
        'input[aria-label*="location" i]',
        'input[aria-label*="search" i]',
        'input[name*="search" i]',
        'input[name*="location" i]',
        'input[name*="zip" i]',
        'input[id*="search" i]',
        'input[id*="location" i]',
        'input[type="search"]',
    ]

    for sel in search_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                logger.info("Found search input: %s — entering broad search", sel)
                # Try a central US zip code to trigger a broad search
                search_terms = ["60601", "10001", "90001", "77001", "85001"]
                for term in search_terms:
                    el.click()
                    time.sleep(0.3)
                    el.fill("")
                    el.fill(term)
                    time.sleep(0.3)
                    page.keyboard.press("Enter")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        time.sleep(3)
                    time.sleep(1)
                return
        except Exception:
            continue


def _try_load_all(page):
    """Click 'show all', 'view all', 'load more' buttons if present."""
    button_patterns = [
        'button:has-text("view all")',
        'button:has-text("show all")',
        'button:has-text("see all")',
        'a:has-text("view all")',
        'a:has-text("show all")',
        'a:has-text("see all")',
    ]

    for sel in button_patterns:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                logger.info("Clicking 'load all' button: %s", sel)
                el.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    time.sleep(3)
                return
        except Exception:
            continue

    # Also try clicking "load more" repeatedly
    load_more_sel = [
        'button:has-text("load more")',
        'button:has-text("show more")',
        'a:has-text("load more")',
        'a:has-text("show more")',
    ]
    for sel in load_more_sel:
        for _ in range(20):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    time.sleep(1.5)
                else:
                    break
            except Exception:
                break


# ── Nationwide zip sweep for search-based locators ─────────────────────

# Param names that typically carry a zip / postal code in locator APIs.
_ZIP_PARAM_NAMES = {
    "zip", "zipcode", "postal", "postalcode", "postal_code", "postcode",
    "place", "location", "address", "q", "query", "search", "where",
    "near", "geo", "city",
}
_LAT_PARAM_NAMES = {"lat", "latitude", "y"}
_LNG_PARAM_NAMES = {"lng", "lon", "long", "longitude", "x"}


def _looks_like_locator_api(url: str) -> bool:
    """Best-effort: does this URL look like a store-search API?"""
    lower = url.lower()
    return any(kw in lower for kw in (
        "/locations", "/stores", "/store-locator", "/store-finder",
        "/storefinder", "/storelocator", "/store/", "/locator",
        "/apiproxy/", "/find-a-store", "/find/", "/branches",
        "/restaurants", "/find-store", "/find-location",
    ))


def _identify_sweep_template(intercepted: list[dict]) -> dict | None:
    """
    Look through intercepted API calls for one that's a search-based
    locator (URL has a zip/place param). Return a dict with the URL
    template + headers + the param names we substitute, or None.
    """
    for api in intercepted:
        url = api.get("url", "")
        if not _looks_like_locator_api(url):
            continue
        parsed = urlparse(url)
        if not parsed.query:
            continue
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        lower_keys = {k.lower(): k for k in params}

        zip_key = next((lower_keys[k] for k in _ZIP_PARAM_NAMES if k in lower_keys), None)
        if not zip_key:
            continue
        lat_key = next((lower_keys[k] for k in _LAT_PARAM_NAMES if k in lower_keys), None)
        lng_key = next((lower_keys[k] for k in _LNG_PARAM_NAMES if k in lower_keys), None)

        logger.info(
            "Locator API template: %s (zip=%s, lat=%s, lng=%s)",
            url, zip_key, lat_key, lng_key,
        )
        return {
            "parsed": parsed,
            "params": params,
            "zip_key": zip_key,
            "lat_key": lat_key,
            "lng_key": lng_key,
            "headers": api.get("request_headers", {}),
            "sample_url": url,
        }
    return None


def _build_sweep_url(template: dict, zip_code: str, lat: float | None, lng: float | None) -> str:
    """Rebuild the URL with a different zip / lat / lng."""
    parsed = template["parsed"]
    params = dict(template["params"])
    params[template["zip_key"]] = zip_code
    if lat is not None and template["lat_key"]:
        params[template["lat_key"]] = f"{lat:.7f}"
    if lng is not None and template["lng_key"]:
        params[template["lng_key"]] = f"{lng:.7f}"
    new_query = urlencode(params, doseq=True)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, parsed.fragment,
    ))


def _build_session_from_cookies(cookies: list[dict], headers: dict) -> requests.Session:
    sess = requests.Session()
    for c in cookies:
        try:
            sess.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        except Exception:
            continue

    # Carry over only headers that won't break requests' auto-handling
    skip = {"host", "content-length", "connection", "accept-encoding",
            ":authority", ":method", ":path", ":scheme"}
    for k, v in (headers or {}).items():
        if k.lower().startswith(":") or k.lower() in skip:
            continue
        try:
            sess.headers[k] = v
        except Exception:
            pass
    return sess


def _sweep_us_zips(intercepted: list[dict], cookies: list[dict]) -> list[dict]:
    """
    If one of the intercepted API calls is a search-based locator (returns
    nearby stores per zip), replay it across the curated US zip grid.

    Returns extra response dicts in the same shape as intercepted_apis so
    the extractor can pick them up.
    """
    template = _identify_sweep_template(intercepted)
    if not template:
        return []

    try:
        from scraper.us_zips import US_ZIPS
    except ImportError:
        logger.debug("us_zips module missing; skipping sweep")
        return []

    try:
        import pgeocode
        nomi = pgeocode.Nominatim("us")
    except ImportError:
        logger.warning("pgeocode not installed; sweep will use zip-only (less accurate)")
        nomi = None

    # Determine which zips we've already covered (from the manual searches)
    already = set()
    zip_key = template["zip_key"]
    for api in intercepted:
        try:
            q = dict(parse_qsl(urlparse(api["url"]).query))
            if q.get(zip_key):
                already.add(q[zip_key])
        except Exception:
            continue

    to_sweep = [z for z in US_ZIPS if z not in already]
    logger.info(
        "Sweeping %d additional US zips (%d already covered)",
        len(to_sweep), len(already),
    )

    sess = _build_session_from_cookies(cookies, template["headers"])

    def _fetch(zip_code: str, attempt: int = 0) -> dict | None:
        lat = lng = None
        if nomi is not None:
            try:
                r = nomi.query_postal_code(zip_code)
                if r is not None and r.latitude == r.latitude:  # not NaN
                    lat = float(r.latitude)
                    lng = float(r.longitude)
            except Exception:
                pass
        url = _build_sweep_url(template, zip_code, lat, lng)
        try:
            resp = sess.get(url, timeout=15)
        except requests.RequestException:
            return None
        # Treat 429 / 5xx as transient — retry up to twice with backoff
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 + attempt * 2)
            return _fetch(zip_code, attempt + 1)
        if resp.status_code != 200:
            return None
        body = resp.text
        if len(body) < 100:
            return None
        s = body.lstrip()
        if not (s.startswith("{") or s.startswith("[")):
            return None
        return {
            "url": url,
            "status": 200,
            "body": body,
            "method": "GET",
            "request_headers": template["headers"],
        }

    results: list[dict] = []
    # 4 workers is a sweet spot: fast enough to finish in 2-3 min but slow
    # enough to avoid most rate limiters. With retry-on-429, we recover from
    # transient throttles automatically.
    workers = 4
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, z): z for z in to_sweep}
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if i % 50 == 0:
                logger.info("Sweep progress: %d/%d zips, %d hits", i, len(to_sweep), len(results))

    logger.info("Sweep complete: %d/%d zip responses captured", len(results), len(to_sweep))
    return results


def _render_static(url: str) -> RenderResult | None:
    logger.info("Attempting static render: %s", url)
    try:
        resp = requests.get(url, headers=_get_headers(), timeout=20, allow_redirects=True)
        resp.raise_for_status()

        if _looks_js_rendered(resp.text):
            logger.info("Page appears to be JS-rendered, will try Playwright")
            return None

        logger.info("Static render successful (%d bytes)", len(resp.text))
        return RenderResult(
            html=resp.text,
            final_url=str(resp.url),
            method="static",
            status_code=resp.status_code,
        )
    except requests.RequestException as e:
        logger.warning("Static request failed: %s", e)
        return None


def _render_playwright(url: str) -> RenderResult | None:
    logger.info("Attempting Playwright render: %s", url)
    intercepted = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser_type = getattr(p, BROWSER, p.chromium)
            browser = browser_type.launch(headless=True)
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=random.choice(USER_AGENTS),
                java_script_enabled=True,
            )

            page = context.new_page()

            # Intercept ALL JSON responses to find location data.
            # We also keep the request's headers so we can replay the API
            # ourselves (e.g. for nationwide zip sweeps).
            def handle_response(response):
                req_url = response.url
                content_type = response.headers.get("content-type", "")
                is_json = "json" in content_type or "javascript" in content_type
                is_known_api = _is_api_endpoint(req_url)

                if not (is_json or is_known_api):
                    return
                if response.status != 200:
                    return
                if any(req_url.endswith(ext) for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff"]):
                    return

                try:
                    body = response.text()
                    if len(body) > 500 and (body.lstrip().startswith("{") or body.lstrip().startswith("[")):
                        try:
                            req_headers = dict(response.request.headers)
                        except Exception:
                            req_headers = {}
                        intercepted.append({
                            "url": req_url,
                            "status": response.status,
                            "body": body,
                            "method": response.request.method,
                            "request_headers": req_headers,
                        })
                        logger.debug("Intercepted JSON response: %s (%d bytes)", req_url, len(body))
                except Exception:
                    pass

            page.on("response", handle_response)

            # Disable webdriver detection
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            # Use domcontentloaded (not networkidle) so JS-heavy sites that
            # keep making background requests don't time us out. We then wait
            # briefly for additional XHRs to fire, which is when locator APIs
            # typically resolve.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning("page.goto raised but continuing with what loaded: %s", e)

            # Give XHRs/fetches a moment to complete after DOMContentLoaded.
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # OK — we'll still try to extract from whatever fired

            # Simulate human-like behavior
            try:
                page.mouse.move(
                    random.randint(100, VIEWPORT["width"] - 100),
                    random.randint(100, VIEWPORT["height"] - 100),
                )
            except Exception:
                pass
            time.sleep(random.uniform(1, 2))

            # Try to interact with search-based locators
            try:
                _try_locator_search(page)
            except Exception as e:
                logger.debug("locator search interaction failed: %s", e)

            # Scroll down to trigger lazy loading
            for frac in (1/3, 2/3, 1.0):
                try:
                    page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {frac})")
                    time.sleep(1)
                except Exception:
                    break

            # Click "show all" / "view all" / "load more" if present
            try:
                _try_load_all(page)
            except Exception as e:
                logger.debug("load-all interaction failed: %s", e)

            try:
                html = page.content()
            except Exception:
                html = ""
            try:
                final_url = page.url
            except Exception:
                final_url = url

            # ── Nationwide sweep for search-based locators ───────────────
            # If we captured a locator-shaped API call during the manual
            # zip search, replay it across the full US zip grid using the
            # browser's cookies. This is what gets us coverage for chains
            # like Starbucks (15K stores) where each search only returns
            # nearby results.
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []

            try:
                swept = _sweep_us_zips(intercepted, cookies)
                if swept:
                    intercepted.extend(swept)
                    logger.info(
                        "Nationwide sweep added %d responses (total intercepted: %d)",
                        len(swept), len(intercepted),
                    )
            except Exception as e:
                logger.warning("Nationwide sweep failed: %s", e)

            browser.close()

            logger.info(
                "Playwright render successful (%d bytes, %d API calls intercepted)",
                len(html), len(intercepted),
            )
            return RenderResult(
                html=html,
                final_url=final_url,
                method="playwright",
                intercepted_apis=intercepted,
            )
    except Exception as e:
        logger.error("Playwright render failed: %s", e)
        return None


def render_page(url: str, force_playwright: bool = False) -> RenderResult | None:
    """
    Render a page, trying static first, falling back to Playwright.
    Returns RenderResult or None if both methods fail.
    """
    if not force_playwright:
        result = _render_static(url)
        if result:
            return result
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    result = _render_playwright(url)
    if result:
        return result

    logger.error("All rendering methods failed for %s", url)
    return None
