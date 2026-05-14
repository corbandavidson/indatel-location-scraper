import json
import re
import logging
import random
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from config.settings import (
    LOCATION_JSON_KEYS,
    USER_AGENTS,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
)
from scraper.renderer import RenderResult

logger = logging.getLogger("scraper.extractor")

ADDR_FIELDS = {
    "street_address", "address", "streetAddress", "street", "address1", "addressLine1",
    "addr", "street_addr", "line1", "streetAndNumber", "street_and_number",
    "address_line_1", "addressLine", "stAddr",
    # Starbucks-style
    "streetAddressLine1", "street_address_line_1",
}
CITY_FIELDS = {
    "city", "locality", "town", "municipality",
    "addressLocality", "address_locality",
}
STATE_FIELDS = {
    "state", "region", "province", "administrativeArea", "state_code", "stateProvince",
    "countrySubdivisionCode", "country_subdivision_code", "stateCode",
    "addressRegion", "address_region",
}
ZIP_FIELDS = {"zip", "zipCode", "zip_code", "postalCode", "postal_code", "postcode"}
COUNTRY_FIELDS = {
    "country", "countryCode", "country_code",
    "addressCountry", "address_country",
}
PHONE_FIELDS = {"phone", "phoneNumber", "phone_number", "telephone", "tel"}
LAT_FIELDS = {"lat", "latitude", "geo_lat", "y"}
LNG_FIELDS = {"lng", "longitude", "lon", "geo_lng", "geo_lon", "x"}
NAME_FIELDS = {
    "name", "locationName", "location_name", "storeName", "store_name",
    "title", "displayName", "display_name",
}
HOURS_FIELDS = {
    "hours", "hoursOfOperation", "hours_of_operation", "openingHours",
    "opening_hours", "storeHours", "store_hours", "operatingHours",
}
TYPE_FIELDS = {
    "type", "locationType", "location_type", "storeType", "store_type",
    "category", "format",
}
URL_FIELDS = {"url", "link", "href", "pageUrl", "page_url", "detailUrl", "detail_url", "slug"}


def _find_field(data: dict, field_names: set) -> str | None:
    for key in field_names:
        # Direct key match
        if key in data:
            val = data[key]
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, (int, float)):
                return str(val)
        # Case-insensitive match
        for dk in data:
            if dk.lower() == key.lower():
                val = data[dk]
                if isinstance(val, str):
                    return val.strip()
                if isinstance(val, (int, float)):
                    return str(val)
    return None


def _find_nested_field(data: dict, field_names: set) -> str | None:
    result = _find_field(data, field_names)
    if result:
        return result
    # Search one level deep in nested dicts
    for val in data.values():
        if isinstance(val, dict):
            result = _find_field(val, field_names)
            if result:
                return result
    return None


_LOCATION_WRAPPER_KEYS = ("store", "location", "place", "branch", "node", "fields", "attributes")


def _unwrap_location(raw):
    """
    Many APIs wrap each store under a single key: {"store": {...}}, {"node": {...}},
    {"fields": {...}}, etc. If the input is a dict with exactly one such wrapper
    key whose value is a dict, return the inner dict. Otherwise return as-is.
    """
    if not isinstance(raw, dict):
        return raw
    if len(raw) == 1:
        only_key, only_val = next(iter(raw.items()))
        if only_key in _LOCATION_WRAPPER_KEYS and isinstance(only_val, dict):
            return only_val
    # Also handle the case where the dict has the wrapper key alongside metadata
    # but the wrapper key clearly holds the actual record.
    for wk in _LOCATION_WRAPPER_KEYS:
        v = raw.get(wk)
        if isinstance(v, dict) and len(v) > 3:
            # Heuristic: if the wrapped dict has plausibly more fields than
            # the outer dict, it's the real record.
            if len(v) > len(raw) - 1:
                return v
    return raw


def _parse_location_dict(raw: dict, source_url: str) -> dict | None:
    """Extract a normalized location record from a raw dict."""

    raw = _unwrap_location(raw)
    if not isinstance(raw, dict):
        return None

    # Handle nested address objects (Schema.org style)
    addr_obj = raw.get("address") or raw.get("Address") or {}
    if isinstance(addr_obj, str):
        # address is a flat string
        flat_addr = addr_obj
        addr_obj = {}
    else:
        flat_addr = None

    combined = {**raw}
    if isinstance(addr_obj, dict):
        combined.update(addr_obj)

    street = _find_nested_field(combined, ADDR_FIELDS) or flat_addr
    city = _find_nested_field(combined, CITY_FIELDS)
    state = _find_nested_field(combined, STATE_FIELDS)
    zipcode = _find_nested_field(combined, ZIP_FIELDS)

    if not street and not city and not zipcode:
        return None

    geo = raw.get("geo") or raw.get("coordinates") or raw.get("geoCoordinates") or {}
    if isinstance(geo, dict):
        combined_geo = {**combined, **geo}
    else:
        combined_geo = combined

    location = {
        "location_name": _find_nested_field(combined, NAME_FIELDS) or "",
        "street_address": street or "",
        "city": city or "",
        "state": state or "",
        "zip_code": zipcode or "",
        "country": _find_nested_field(combined, COUNTRY_FIELDS) or "",
        "phone_number": _find_nested_field(combined, PHONE_FIELDS) or "",
        "hours_of_operation": _find_nested_field(combined, HOURS_FIELDS) or "",
        "location_type": _find_nested_field(combined, TYPE_FIELDS) or "",
        "latitude": _find_nested_field(combined_geo, LAT_FIELDS) or "",
        "longitude": _find_nested_field(combined_geo, LNG_FIELDS) or "",
        "location_url": _find_nested_field(combined, URL_FIELDS) or "",
        "source_url": source_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    # Handle hours as list/dict
    if not location["hours_of_operation"]:
        for hf in HOURS_FIELDS:
            val = combined.get(hf)
            if isinstance(val, list):
                location["hours_of_operation"] = "; ".join(
                    str(h) if not isinstance(h, dict) else
                    f"{h.get('day', h.get('dayOfWeek', ''))}: {h.get('opens', h.get('open', ''))}-{h.get('closes', h.get('close', ''))}"
                    for h in val
                )
                break

    return location


def _looks_like_location_list(data) -> bool:
    if not isinstance(data, list) or len(data) < 2:
        return False
    if not isinstance(data[0], dict):
        return False
    # Unwrap a single-key wrapper like {"store": {...}} so wrapped lists
    # are recognized.
    first = _unwrap_location(data[0])
    if not isinstance(first, dict):
        return False
    keys_lower = {k.lower() for k in first}
    match_count = sum(1 for k in LOCATION_JSON_KEYS if k.lower() in keys_lower)
    return match_count >= 2


def _find_location_arrays(obj, depth=0) -> list[list]:
    """Recursively find arrays that look like location data."""
    if depth > 8:
        return []
    results = []
    if isinstance(obj, list) and _looks_like_location_list(obj):
        results.append(obj)
    elif isinstance(obj, dict):
        for val in obj.values():
            results.extend(_find_location_arrays(val, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_location_arrays(item, depth + 1))
    return results


# ─── Strategy 1: JSON-LD / Schema.org ────────────────────────────────

def extract_jsonld(html: str, source_url: str) -> list[dict]:
    logger.info("Strategy 1: Trying JSON-LD extraction")
    locations = []
    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            ld_type = data.get("@type", "")
            if isinstance(ld_type, list):
                ld_type = " ".join(ld_type)
            if any(t in ld_type for t in [
                "LocalBusiness", "Store", "Restaurant", "BankOrCreditUnion",
                "Place", "Hotel", "MedicalBusiness", "FinancialService",
            ]):
                items = [data]
            elif data.get("@graph"):
                items = [
                    item for item in data["@graph"]
                    if isinstance(item, dict) and any(
                        t in str(item.get("@type", ""))
                        for t in ["LocalBusiness", "Store", "Restaurant", "Place"]
                    )
                ]

        for item in items:
            loc = _parse_location_dict(item, source_url)
            if loc:
                locations.append(loc)

    if locations:
        logger.info("JSON-LD found %d locations", len(locations))
    return locations


# ─── Strategy 2: Embedded JSON data ──────────────────────────────────

def extract_embedded_json(html: str, source_url: str) -> list[dict]:
    logger.info("Strategy 2: Trying embedded JSON extraction")
    locations = []
    soup = BeautifulSoup(html, "lxml")

    # Parse __NEXT_DATA__ tag directly (more reliable than regex for large JSON)
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        try:
            data = json.loads(next_data_tag.string)
            arrays = _find_location_arrays(data)
            for arr in arrays:
                for item in arr:
                    loc = _parse_location_dict(item, source_url)
                    if loc:
                        locations.append(loc)
            if locations:
                logger.info("__NEXT_DATA__ found %d locations", len(locations))
                return locations
        except json.JSONDecodeError:
            pass

    # Regex patterns for window-assigned JSON
    patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.+?});?\s*(?:</script>|\n)',
        r'window\.__NUXT__\s*=\s*({.+?});?\s*(?:</script>|\n)',
        r'window\.PAGE_DATA\s*=\s*({.+?});?\s*(?:</script>|\n)',
        r'window\.__DATA__\s*=\s*({.+?});?\s*(?:</script>|\n)',
        r'var\s+(?:stores|locations|storeData|locationData)\s*=\s*(\[.+?\]);?\s*(?:</script>|\n)',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, html, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                arrays = _find_location_arrays(data)
                for arr in arrays:
                    for item in arr:
                        loc = _parse_location_dict(item, source_url)
                        if loc:
                            locations.append(loc)
            except (json.JSONDecodeError, IndexError):
                continue

    # Search all script tags for JSON arrays that look like location data
    for script in soup.find_all("script"):
        if not script.string or len(script.string) < 200:
            continue
        # Skip already-parsed __NEXT_DATA__
        if script.get("id") == "__NEXT_DATA__":
            continue
        # Try to find JSON objects/arrays directly
        for match in re.finditer(r'(\[\s*\{[^<]{50,}?\}\s*\])', script.string, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                if _looks_like_location_list(data):
                    for item in data:
                        loc = _parse_location_dict(item, source_url)
                        if loc:
                            locations.append(loc)
            except json.JSONDecodeError:
                continue

    if locations:
        logger.info("Embedded JSON found %d locations", len(locations))
    return locations


# ─── Strategy 3: API endpoint sniffing ────────────────────────────────

def extract_from_apis(intercepted_apis: list[dict], source_url: str) -> list[dict]:
    if not intercepted_apis:
        return []
    logger.info("Strategy 3: Trying %d intercepted API responses", len(intercepted_apis))
    locations = []

    for api in intercepted_apis:
        try:
            data = json.loads(api["body"])
        except (json.JSONDecodeError, KeyError):
            continue

        arrays = _find_location_arrays(data)
        if not arrays and isinstance(data, dict):
            # Try common wrapper keys
            for key in ["results", "data", "locations", "stores", "items", "response"]:
                wrapped = data.get(key)
                if wrapped:
                    arrays = _find_location_arrays(wrapped) if isinstance(wrapped, dict) else (
                        [wrapped] if _looks_like_location_list(wrapped) else []
                    )
                    if arrays:
                        break

        for arr in arrays:
            for item in arr:
                loc = _parse_location_dict(item, source_url)
                if loc:
                    loc["source_url"] = api.get("url", source_url)
                    locations.append(loc)

    if locations:
        logger.info("API sniffing found %d locations", len(locations))
    return locations


# ─── Strategy 4: HTML parsing ─────────────────────────────────────────

def _extract_address_from_text(text: str) -> dict | None:
    """Try to parse a street address, city, state, zip from a text block."""
    try:
        import usaddress
        tagged, _ = usaddress.tag(text)
        street_parts = []
        for key in ["AddressNumber", "StreetNamePreDirectional", "StreetName",
                     "StreetNamePostType", "StreetNamePostDirectional",
                     "SubaddressType", "SubaddressIdentifier", "OccupancyType",
                     "OccupancyIdentifier"]:
            if key in tagged:
                street_parts.append(tagged[key])
        return {
            "street_address": " ".join(street_parts),
            "city": tagged.get("PlaceName", ""),
            "state": tagged.get("StateName", ""),
            "zip_code": tagged.get("ZipCode", ""),
        }
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback regex parsing
    match = re.search(
        r'(\d+\s+.+?),\s*([A-Za-z\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
        text,
    )
    if match:
        return {
            "street_address": match.group(1).strip(),
            "city": match.group(2).strip(),
            "state": match.group(3).strip(),
            "zip_code": match.group(4).strip(),
        }
    return None


def extract_html_locations(html: str, source_url: str) -> list[dict]:
    logger.info("Strategy 4: Trying HTML parsing")
    locations = []
    soup = BeautifulSoup(html, "lxml")

    # Look for common location card patterns
    selectors = [
        '[class*="location"]', '[class*="store"]', '[class*="branch"]',
        '[data-type="location"]', '[data-type="store"]',
        '[itemtype*="LocalBusiness"]', '[itemtype*="Place"]',
        '.location-card', '.store-card', '.location-item', '.store-item',
        '.directory-item', '.results-item', '.listing',
    ]

    cards = []
    for sel in selectors:
        found = soup.select(sel)
        if found and len(found) >= 2:
            cards = found
            logger.debug("Found %d cards with selector '%s'", len(found), sel)
            break

    if not cards:
        # Try finding address-like elements
        address_tags = soup.find_all("address")
        if address_tags:
            cards = [tag.parent or tag for tag in address_tags]

    now = datetime.now(timezone.utc).isoformat()
    for card in cards:
        text = card.get_text(separator="\n", strip=True)
        if len(text) < 10:
            continue

        addr_data = _extract_address_from_text(text)
        if not addr_data or not any(addr_data.values()):
            continue

        # Try to find phone
        phone_match = re.search(r'\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', text)
        phone = phone_match.group(0) if phone_match else ""

        # Try to find name (often the first line or a heading)
        heading = card.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
        name = heading.get_text(strip=True) if heading else ""

        # Try to find a link
        link = card.find("a", href=True)
        loc_url = link["href"] if link else ""
        if loc_url and not loc_url.startswith("http"):
            from urllib.parse import urljoin
            loc_url = urljoin(source_url, loc_url)

        locations.append({
            "location_name": name,
            "street_address": addr_data.get("street_address", ""),
            "city": addr_data.get("city", ""),
            "state": addr_data.get("state", ""),
            "zip_code": addr_data.get("zip_code", ""),
            "country": "",
            "phone_number": phone,
            "hours_of_operation": "",
            "location_type": "",
            "latitude": "",
            "longitude": "",
            "location_url": loc_url,
            "source_url": source_url,
            "scraped_at": now,
        })

    if locations:
        logger.info("HTML parsing found %d locations", len(locations))
    return locations


# ─── Strategy 5: Sitemap fallback ─────────────────────────────────────

def _fetch_with_fallback(url: str, headers: dict, session: requests.Session | None = None) -> str | None:
    """
    Fetch a URL via requests first; if blocked (403/503/Cloudflare challenge),
    fall back to Playwright. This lets us read sitemap.xml on sites like
    Casey's that block static requests but render fine in a browser.
    """
    try:
        sess = session or requests
        resp = sess.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and "<title>Just a moment" not in resp.text[:500]:
            return resp.text
    except requests.RequestException:
        pass

    # Fallback: Playwright
    try:
        from playwright.sync_api import sync_playwright
        from config.settings import USER_AGENTS as _UAS, BROWSER as _BROWSER
        logger.info("Sitemap fetch falling back to Playwright for %s", url)
        with sync_playwright() as p:
            btype = getattr(p, _BROWSER, p.chromium)
            browser = btype.launch(headless=True)
            ctx = browser.new_context(user_agent=random.choice(_UAS))
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            content = page.content()
            browser.close()
            # Strip the <html><body>...<pre>...</pre> wrapper Chrome adds around raw XML
            m = re.search(r"<pre[^>]*>(.*?)</pre>", content, re.DOTALL)
            if m:
                content = m.group(1)
            return content
    except Exception as e:
        logger.debug("Playwright sitemap fetch failed: %s", e)
        return None


# ── Strategy 4.5: Directory-tree crawl (Yext/Uberall-style sites) ─────

_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn",
    "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh",
    "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa",
    "wv", "wi", "wy",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new-hampshire", "new-jersey",
    "new-mexico", "new-york", "north-carolina", "north-dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode-island", "south-carolina",
    "south-dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "washington-dc", "west-virginia", "wisconsin", "wyoming",
}


def _is_state_segment(seg: str) -> bool:
    s = seg.lower().strip("/")
    return s in _US_STATE_CODES or s in _US_STATE_NAMES


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _same_origin_links(html: str, base_url: str) -> list[str]:
    """All same-origin hrefs from a page, normalized to absolute, deduped.
    Uses regex (not BeautifulSoup) since this is called for many large pages
    and BS4 is GIL-bound on 1MB+ HTML."""
    from urllib.parse import urlparse, urljoin
    parsed = urlparse(base_url)
    out: set[str] = set()
    skip_ext = (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".gif")
    for raw in _HREF_RE.findall(html):
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        u = urljoin(base_url, raw).split("#")[0]
        p = urlparse(u)
        if p.netloc != parsed.netloc:
            continue
        if u.lower().endswith(skip_ext):
            continue
        out.add(u)
    return sorted(out)


def _path_segments(url: str) -> list[str]:
    from urllib.parse import urlparse
    return [s for s in urlparse(url).path.split("/") if s and not s.endswith((".html", ".htm"))]


def _looks_like_state_directory(html: str, base_url: str) -> list[str]:
    """
    Detect a state-level directory landing page. Returns the list of state-level
    URLs if found, otherwise empty list.

    A page qualifies if it has ≥20 same-origin links whose path's last segment
    is a US state code or name.
    """
    candidates: list[str] = []
    for u in _same_origin_links(html, base_url):
        segs = _path_segments(u)
        if not segs:
            continue
        if _is_state_segment(segs[-1]):
            candidates.append(u)
    # Dedupe by final segment (state)
    by_state: dict[str, str] = {}
    for u in candidates:
        st = _path_segments(u)[-1].lower()
        by_state.setdefault(st, u)
    if len(by_state) >= 20:
        return sorted(by_state.values())
    return []


# Yext/Uberall directory pages typically render link text like
# "Alabama(116)" or "Alabama (116 stores)" or "Alabama 116". Pull the
# count out so we can estimate the total store count up front.
_COUNT_FROM_LINK_RE = re.compile(
    r'>([^<]*?\(?(\d{1,5})\)?(?:\s+stores?)?)\s*<\s*/\s*a\s*>',
    re.IGNORECASE,
)


def _estimate_store_count(html: str, state_urls: list[str]) -> int:
    """
    Parse the state-directory HTML for per-state store counts and sum them.
    Returns 0 if no counts could be extracted.
    """
    # Build a regex that matches anchor tags whose href points to one of the
    # state URLs. We look for a number immediately before </a>.
    state_paths = set()
    for u in state_urls:
        try:
            from urllib.parse import urlparse
            p = urlparse(u).path.rstrip("/")
            if p:
                state_paths.add(p)
        except Exception:
            continue

    total = 0
    # Iterate every anchor in the page; if its href matches a state, capture
    # any trailing numeric annotation in the text.
    anchor_re = re.compile(
        r'<a\s+[^>]*?href=["\']([^"\']+)["\'][^>]*>([^<]+?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    seen_states: set[str] = set()
    for m in anchor_re.finditer(html):
        href, text = m.group(1), m.group(2)
        # Match href to one of the state paths (suffix match handles
        # relative vs absolute URLs).
        if not any(href.rstrip("/").endswith(p.lstrip("/")) or href.endswith(p) for p in state_paths):
            continue
        # State key dedupe so multi-count anchors for one state don't double-add
        key = href.lower().rstrip("/")
        if key in seen_states:
            continue
        # Find the last number in the link text
        nums = re.findall(r"\d+", text)
        if not nums:
            continue
        try:
            total += int(nums[-1])
            seen_states.add(key)
        except ValueError:
            continue
    return total
    return []


_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]*?type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Cheap pre-filter: is there even a store-shaped JSON-LD block on this page?
# If not, we can skip the (relatively expensive) JSON parse entirely.
_STORE_TYPE_HINT_RE = re.compile(
    r'"@type"\s*:\s*"(Store|LocalBusiness|Restaurant|FoodEstablishment|'
    r'GroceryStore|GeneralStore|Pharmacy|GasStation|BankOrCreditUnion|'
    r'ConvenienceStore|DepartmentStore|ShoppingCenter|Place)"'
)

_STORE_TYPES = {
    "Store", "LocalBusiness", "Place", "Restaurant", "FoodEstablishment",
    "GroceryStore", "GeneralStore", "Pharmacy", "GasStation",
    "BankOrCreditUnion", "ConvenienceStore", "DepartmentStore",
    "ShoppingCenter", "Organization",
}


def _extract_store_jsonld(html: str, page_url: str) -> dict | None:
    """Pull a single store record from a leaf page's JSON-LD.

    Uses regex to find the `<script type="application/ld+json">` blocks
    instead of BeautifulSoup — BS4 has to build a full DOM for a 1.5 MB
    page, which dominates wall time when crawling thousands of pages.
    """
    if not _STORE_TYPE_HINT_RE.search(html):
        return None
    for block in _JSONLD_BLOCK_RE.findall(html):
        text = block.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        items = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
            has_addr = isinstance(item.get("address"), dict)
            if not (any(tt in _STORE_TYPES for tt in types) or has_addr):
                continue
            loc = _parse_location_dict(item, page_url)
            if loc and (loc.get("street_address") or loc.get("zip_code")):
                return loc
    return None


# Match "1234 Main St, Springfield, IL 60601" style addresses (with or without
# surrounding markup) — used as a fallback when a store page has no JSON-LD
# (e.g. Sprouts puts the address in plain HTML).
_PLAIN_ADDRESS_RE = re.compile(
    r"(\d{1,6}\s+[A-Za-z][A-Za-z0-9\.\-' ]{2,60}?)"      # street
    r"[,\s]+([A-Za-z][A-Za-z\.\-' ]{1,40}?)"              # city
    r",?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"               # state + zip
)


def _extract_store_text(html: str, page_url: str) -> dict | None:
    """
    Fallback when no JSON-LD is present: scan the page text for an address.
    Sprouts is a real-world example — each store page has the address as
    plain HTML text without Schema.org markup.
    """
    # Strip tags cheaply
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    m = _PLAIN_ADDRESS_RE.search(text)
    if not m:
        return None

    # Try to extract a friendly name from the page title
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    name = title_m.group(1).strip().split("|")[0].split(" - ")[0].strip() if title_m else ""

    phone_m = re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text)
    return {
        "location_name": name,
        "street_address": m.group(1).strip(),
        "city": m.group(2).strip(),
        "state": m.group(3).strip(),
        "zip_code": m.group(4).strip(),
        "country": "US",
        "phone_number": phone_m.group(0) if phone_m else "",
        "hours_of_operation": "",
        "location_type": "",
        "latitude": "",
        "longitude": "",
        "location_url": page_url,
        "source_url": page_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_store_any(html: str, page_url: str) -> dict | None:
    """JSON-LD first (fast & high-quality), text fallback otherwise."""
    return _extract_store_jsonld(html, page_url) or _extract_store_text(html, page_url)


def extract_from_directory_tree(html: str, base_url: str, progress_cb=None) -> list[dict]:
    """
    Strategy 4.5: Walk a Yext/Uberall-style state→city→store directory.
    Many chains (Dollar Tree, Walgreens, McDonald's, Subway, 7-Eleven, KFC,
    AT&T, T-Mobile, etc.) host their locator on a subdomain like
    locations.{brand}.com with a clean directory of state/city pages and
    JSON-LD on each store page. This crawler walks that tree in parallel
    via HTTP, no Playwright needed.

    progress_cb, if provided, is called as progress_cb(phase, current, total)
    so a UI can show live progress for what may be a long-running crawl.

    Returns [] if neither the current page nor `/index.html` on the same
    host looks like a state directory.
    """
    def _emit(phase, current, total):
        if progress_cb:
            try:
                progress_cb(phase, current, total)
            except Exception:
                pass

    state_urls = _looks_like_state_directory(html, base_url)
    directory_html = html
    if not state_urls:
        # Common case: the rendered page is /store-locator (a search UI), but
        # the same host also has /index.html with the state tree. Probe it.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(base_url)
        if "locations" in parsed.netloc or "stores" in parsed.netloc:
            index_url = urlunparse((parsed.scheme, parsed.netloc, "/index.html", "", "", ""))
            try:
                r = requests.get(index_url,
                                 headers={"User-Agent": random.choice(USER_AGENTS)},
                                 timeout=12, allow_redirects=True)
                if r.status_code == 200 and len(r.text) > 1000:
                    state_urls = _looks_like_state_directory(r.text, index_url)
                    if state_urls:
                        base_url = index_url
                        directory_html = r.text
                        logger.info("Found state directory at %s", index_url)
            except requests.RequestException:
                pass
    if not state_urls:
        return []

    # Estimate total store count from the state-page link text annotations.
    expected_total = _estimate_store_count(directory_html, state_urls)
    if expected_total:
        logger.info("Directory crawl: estimated %d total locations", expected_total)
        _emit("estimate", expected_total, expected_total)

    logger.info("Strategy 4.5: Directory tree detected — %d states", len(state_urls))

    headers = {"User-Agent": random.choice(USER_AGENTS)}
    sess = requests.Session()
    sess.headers.update(headers)
    # Match the connection pool size to our max worker count so threads don't
    # discard and re-establish TLS connections every request.
    from requests.adapters import HTTPAdapter
    adapter = HTTPAdapter(pool_connections=80, pool_maxsize=80, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    _JSONLD_QUICK_RE = re.compile(
        r'application/ld\+json["\'][^>]*>([^<]+)<', re.IGNORECASE | re.DOTALL
    )

    def _quick_has_store_jsonld(html_text: str) -> bool:
        """Cheap check: does this page have JSON-LD with @type containing a
        store-ish term? Avoids running full BeautifulSoup parse on every page."""
        for m in _JSONLD_QUICK_RE.findall(html_text):
            if any(t in m for t in ('"Store"', '"LocalBusiness"', '"Restaurant"',
                                     '"GroceryStore"', '"Pharmacy"', '"GasStation"',
                                     '"DepartmentStore"', '"ConvenienceStore"')):
                return True
        return False

    def _fetch_and_extract_links(u: str, min_depth: int, check_leaf: bool = False) -> tuple[set[str], dict | None]:
        """
        Fetch URL, return (deeper same-origin links, leaf-store JSON-LD if any).

        `min_depth` is the inclusive minimum path-segment depth a link must
        have to be returned. We don't require an exact depth match because
        some chains use different prefixes for the state/store tree (e.g.
        Sprouts: state pages at /stores/ca/ but store pages at
        /store/ca/city/address/).
        """
        try:
            r = sess.get(u, timeout=12)
            if r.status_code != 200:
                return set(), None
            html_text = r.text
        except requests.RequestException:
            return set(), None
        leaf = None
        if check_leaf:
            # Try JSON-LD first; if absent, fall back to text address scan.
            if _quick_has_store_jsonld(html_text):
                leaf = _extract_store_jsonld(html_text, u)
            if not leaf:
                leaf = _extract_store_text(html_text, u)
        out: set[str] = set()
        for link in _same_origin_links(html_text, u):
            if link == u:
                continue
            if len(_path_segments(link)) >= min_depth:
                out.add(link)
        return out, leaf

    def _fetch_and_extract_leaf(u: str) -> dict | None:
        try:
            r = sess.get(u, timeout=12)
            if r.status_code != 200:
                return None
            return _extract_store_any(r.text, u)
        except requests.RequestException:
            return None

    # ── Phase 1: states → city URLs ──────────────────────────────────
    state_depth = len(_path_segments(state_urls[0]))
    city_urls: set[str] = set()
    early_leaves: list[dict] = []
    _emit("states", 0, len(state_urls))
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_and_extract_links, su, state_depth + 1, True): su
                   for su in state_urls}
        for fut in as_completed(futures):
            done += 1
            _emit("states", done, len(state_urls))
            try:
                links, leaf = fut.result()
            except Exception:
                continue
            city_urls.update(links)
            if leaf:
                early_leaves.append(leaf)

    logger.info("Directory crawl: %d cities discovered", len(city_urls))
    if not city_urls and not early_leaves:
        return []

    # ── Phase 2: cities → store URLs ─────────────────────────────────
    city_depth = state_depth + 1
    store_urls: set[str] = set()
    locations: list[dict] = list(early_leaves)
    processed = 0
    _emit("cities", 0, len(city_urls))
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_fetch_and_extract_links, cu, city_depth + 1, True): cu
                   for cu in city_urls}
        for fut in as_completed(futures):
            processed += 1
            if processed % 25 == 0 or processed == len(city_urls):
                _emit("cities", processed, len(city_urls))
            city_url = futures[fut]
            try:
                links, leaf = fut.result()
            except Exception:
                continue
            store_urls.update(links)
            if leaf:
                locations.append(leaf)
            if processed % 500 == 0:
                logger.info("Directory crawl: processed %d/%d cities, %d store URLs queued, %d leaves so far",
                            processed, len(city_urls), len(store_urls), len(locations))

    logger.info("Directory crawl: %d candidate store URLs", len(store_urls))

    # ── Phase 3: stores → JSON-LD ────────────────────────────────────
    if store_urls:
        processed = 0
        _emit("stores", 0, len(store_urls))
        # With regex-based JSON-LD extraction the per-page CPU cost is
        # tiny, so the bottleneck is wall-clock network time. We push
        # parallelism high to keep the pipe saturated.
        with ThreadPoolExecutor(max_workers=64) as pool:
            futures = {pool.submit(_fetch_and_extract_leaf, su): su for su in store_urls}
            for fut in as_completed(futures):
                processed += 1
                if processed % 25 == 0 or processed == len(store_urls):
                    _emit("stores", processed, len(store_urls))
                try:
                    loc = fut.result()
                except Exception:
                    continue
                if loc:
                    locations.append(loc)
                if processed % 1000 == 0:
                    logger.info("Directory crawl: extracted %d locations from %d/%d store pages",
                                len(locations), processed, len(store_urls))

    logger.info("Directory crawl complete: %d locations", len(locations))
    return locations


# ── Strategy 5: Sitemap fallback ──────────────────────────────────────

def extract_from_sitemap(base_url: str) -> list[dict]:
    logger.info("Strategy 5: Trying sitemap fallback")
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    sitemap_urls = [
        f"{domain}/sitemap.xml",
        f"{domain}/sitemap_index.xml",
        f"{domain}/sitemap-locations.xml",
        f"{domain}/sitemap-stores.xml",
        f"{domain}/sitemap_locations.xml",
        f"{domain}/sitemap_stores.xml",
        f"{domain}/sitemap-restaurants.xml",
        f"{domain}/locations-sitemap.xml",
        f"{domain}/stores-sitemap.xml",
        f"{domain}/sitemap-storefinder.xml",
        f"{domain}/sitemap-store-finder.xml",
        f"{domain}/general-store-sitemap.xml",
        f"{domain}/sitemap-general-store.xml",
    ]

    location_page_urls = []
    headers = {"User-Agent": random.choice(USER_AGENTS)}

    for smap_url in sitemap_urls:
        try:
            body = _fetch_with_fallback(smap_url, headers)
            if not body:
                continue
            # Reuse the response object's "text" interface by creating a stub
            class _Stub:
                pass
            resp = _Stub()
            resp.text = body
            resp.status_code = 200
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml-xml")

            # Check for sitemap index
            for sitemap in soup.find_all("sitemap"):
                loc = sitemap.find("loc")
                if loc and any(kw in loc.text.lower() for kw in ["location", "store", "branch", "general-store"]):
                    sub_body = _fetch_with_fallback(loc.text, headers)
                    if not sub_body:
                        continue
                    sub_soup = BeautifulSoup(sub_body, "lxml-xml")
                    for url_tag in sub_soup.find_all("url"):
                        url_loc = url_tag.find("loc")
                        if url_loc:
                            location_page_urls.append(url_loc.text)

            # Direct URL entries
            for url_tag in soup.find_all("url"):
                url_loc = url_tag.find("loc")
                if url_loc:
                    url_text = url_loc.text.lower()
                    if any(kw in url_text for kw in [
                        "/locations/", "/stores/", "/store/", "/branch/",
                        "/restaurants/", "/restaurant/", "/branches/",
                        "/general-store/", "/find-a-store/",
                    ]):
                        # Filter out pure index pages
                        parts = url_text.rstrip("/").split("/")
                        if len(parts) >= 4:
                            location_page_urls.append(url_loc.text)

            if location_page_urls:
                break
        except requests.RequestException:
            continue

    if not location_page_urls:
        logger.info("No location pages found in sitemaps")
        return []

    # De-dupe in case sitemap index pages overlap
    location_page_urls = list(dict.fromkeys(location_page_urls))

    logger.info("Found %d potential location pages in sitemap", len(location_page_urls))
    locations = _scrape_sitemap_pages_parallel(
        location_page_urls[:10000], headers
    )

    if locations:
        logger.info("Sitemap extraction found %d locations", len(locations))
    return locations


def _scrape_sitemap_pages_parallel(urls: list[str], headers: dict, workers: int = 8) -> list[dict]:
    """
    Fetch sitemap-listed location pages concurrently. We're hitting one
    domain repeatedly, so we keep a modest worker pool and short per-request
    timeout. JSON-LD is the only extraction we trust here — if a page doesn't
    have it we fall through to a text-based address heuristic.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []
    sess = requests.Session()
    sess.headers.update(headers)

    def _fetch_one(page_url: str) -> list[dict]:
        body: str | None = None
        try:
            resp = sess.get(page_url, timeout=12)
            if resp.status_code == 200 and "<title>Just a moment" not in resp.text[:500]:
                body = resp.text
        except requests.RequestException:
            pass

        if body is None:
            # Don't fall back to Playwright here — it's far too slow for
            # thousands of pages. If static is blocked, skip silently.
            return []

        page_locs = extract_jsonld(body, page_url)
        if page_locs:
            return page_locs

        # Fallback: extract address from page text
        try:
            page_soup = BeautifulSoup(body, "lxml")
            text = page_soup.get_text(separator="\n", strip=True)
            addr_data = _extract_address_from_text(text)
            if not addr_data or not any(addr_data.values()):
                return []
            title = page_soup.find("title")
            h1 = page_soup.find("h1")
            name = (h1 or title).get_text(strip=True) if (h1 or title) else ""
            phone_match = re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text)
            return [{
                "location_name": name,
                "street_address": addr_data.get("street_address", ""),
                "city": addr_data.get("city", ""),
                "state": addr_data.get("state", ""),
                "zip_code": addr_data.get("zip_code", ""),
                "country": "",
                "phone_number": phone_match.group(0) if phone_match else "",
                "hours_of_operation": "",
                "location_type": "",
                "latitude": "",
                "longitude": "",
                "location_url": page_url,
                "source_url": page_url,
                "scraped_at": now,
            }]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, u): u for u in urls}
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                results.extend(fut.result())
            except Exception:
                pass
            if i % 100 == 0:
                logger.info("Scraping sitemap pages: %d/%d (found %d so far)",
                            i, len(urls), len(results))

    return results


# ─── Orchestrator ─────────────────────────────────────────────────────

def extract_locations(render_result: RenderResult, company_name: str, progress_cb=None) -> list[dict]:
    """
    Try all extraction strategies in priority order, returning the first
    that yields results. `progress_cb`, if provided, is forwarded to the
    directory-tree crawler for long-running phase progress.
    """
    html = render_result.html
    url = render_result.final_url

    # Strategy 1: JSON-LD
    locations = extract_jsonld(html, url)
    if locations:
        logger.info("[%s] JSON-LD extraction: %d locations", company_name, len(locations))
        return locations

    # Strategy 2: Embedded JSON
    locations = extract_embedded_json(html, url)
    if locations:
        logger.info("[%s] Embedded JSON extraction: %d locations", company_name, len(locations))
        return locations

    # Strategy 3: API sniffing (only if Playwright was used)
    if render_result.intercepted_apis:
        locations = extract_from_apis(render_result.intercepted_apis, url)
        if locations:
            logger.info("[%s] API sniffing extraction: %d locations", company_name, len(locations))
            return locations

    # Strategy 3.5 / 4: Directory-tree crawl runs BEFORE generic HTML
    # parsing — state-directory pages often have state cards that the naive
    # HTML card detector mistakes for store cards (e.g. Sprouts returns 26
    # "locations" that are actually one-per-state Alabama/Arkansas… cards).
    locations = extract_from_directory_tree(html, url, progress_cb=progress_cb)
    if locations:
        logger.info("[%s] Directory crawl extraction: %d locations", company_name, len(locations))
        return locations

    # Strategy 4: HTML parsing (only if directory tree didn't apply)
    locations = extract_html_locations(html, url)
    if locations:
        logger.info("[%s] HTML parsing extraction: %d locations", company_name, len(locations))
        return locations

    # Strategy 5: Sitemap fallback
    locations = extract_from_sitemap(url)
    if locations:
        logger.info("[%s] Sitemap extraction: %d locations", company_name, len(locations))
        return locations

    logger.warning("[%s] All extraction strategies exhausted — 0 locations found", company_name)
    return []


# ─── Strategy 6: Direct API probing ──────────────────────────────────

def probe_direct_apis(base_url: str, company_name: str) -> list[dict]:
    """
    Try common API endpoint patterns directly via HTTP, without needing
    Playwright to discover them.
    """
    from urllib.parse import urlparse
    logger.info("Strategy 6: Probing common API endpoints for '%s'", company_name)

    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*",
        "X-Requested-With": "XMLHttpRequest",
    }

    api_paths = [
        "/api/locations",
        "/api/stores",
        "/api/v1/locations",
        "/api/v1/stores",
        "/api/v2/locations",
        "/api/v2/stores",
        "/locations.json",
        "/stores.json",
        "/api/locations/all",
        "/api/stores/all",
        "/api/store-locator",
        "/api/locations?limit=10000",
        "/api/stores?limit=10000",
        "/api/locations?pageSize=10000",
        "/api/locations?count=10000",
        "/rest/locations",
        "/rest/stores",
        "/wp-json/locations/v1/all",
        "/wp-json/wp/v2/locations?per_page=100",
        "/_api/locations",
        "/graphql",  # Will need POST handling
    ]

    # Also try paths relative to the current URL path
    if parsed.path and parsed.path != "/":
        base_path = parsed.path.rstrip("/")
        api_paths.extend([
            f"{base_path}.json",
            f"{base_path}/all",
            f"{base_path}/api",
            f"{base_path}?format=json",
            f"{base_path}?output=json",
        ])

    locations = []
    for path in api_paths:
        url = f"{domain}{path}"
        try:
            time.sleep(random.uniform(0.5, 1.5))
            resp = requests.get(url, headers=headers, timeout=10)

            if resp.status_code != 200:
                continue

            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type and "javascript" not in content_type:
                continue

            try:
                data = resp.json()
            except ValueError:
                continue

            arrays = _find_location_arrays(data)
            if not arrays and isinstance(data, dict):
                for key in ["results", "data", "locations", "stores", "items",
                            "response", "features", "records", "rows"]:
                    wrapped = data.get(key)
                    if isinstance(wrapped, list) and _looks_like_location_list(wrapped):
                        arrays = [wrapped]
                        break
                    if isinstance(wrapped, dict):
                        arrays = _find_location_arrays(wrapped)
                        if arrays:
                            break

            for arr in arrays:
                for item in arr:
                    loc = _parse_location_dict(item, url)
                    if loc:
                        locations.append(loc)

            if locations:
                logger.info("Direct API probe found %d locations at %s", len(locations), url)
                return locations

        except requests.RequestException:
            continue

    logger.info("Direct API probing found no results")
    return locations
