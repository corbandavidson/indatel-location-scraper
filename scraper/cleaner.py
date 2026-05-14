import re
import logging

logger = logging.getLogger("scraper.cleaner")

STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR", "guam": "GU",
    "virgin islands": "VI", "american samoa": "AS",
}


def _normalize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _title_case_safe(text: str) -> str:
    if not text:
        return ""
    if text.isupper() and len(text) <= 2:
        return text
    return text.title()


def _normalize_state(state: str) -> str:
    if not state:
        return ""
    state = state.strip()
    if len(state) == 2:
        return state.upper()
    lookup = state.lower()
    if lookup in STATE_ABBREVIATIONS:
        return STATE_ABBREVIATIONS[lookup]
    return state.title()


def _normalize_zip(zipcode: str) -> str:
    if not zipcode:
        return ""
    zipcode = zipcode.strip()
    match = re.match(r'^(\d{5})(?:-(\d{4}))?', zipcode)
    if match:
        base = match.group(1)
        ext = match.group(2)
        return f"{base}-{ext}" if ext else base
    return zipcode.upper()


def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone.strip()


def _build_full_address(loc: dict) -> str:
    parts = [
        loc.get("street_address", ""),
        loc.get("city", ""),
    ]
    state_zip = " ".join(filter(None, [loc.get("state", ""), loc.get("zip_code", "")]))
    if state_zip:
        parts.append(state_zip)
    country = loc.get("country", "")
    if country and country.upper() not in ("US", "USA", "UNITED STATES"):
        parts.append(country)
    return ", ".join(p for p in parts if p)


def _assess_quality(loc: dict) -> str:
    has_street = bool(loc.get("street_address"))
    has_city = bool(loc.get("city"))
    has_state = bool(loc.get("state"))
    has_zip = bool(loc.get("zip_code"))

    if has_street and has_city and has_state and has_zip:
        return "complete"
    if has_street and (has_city or has_zip):
        return "partial"
    if has_street or has_city or has_zip:
        return "address_only"
    return "empty"


def _dedup_key(loc: dict) -> str:
    street = re.sub(r'\W+', '', (loc.get("street_address") or "").lower())
    zipcode = re.sub(r'\W+', '', (loc.get("zip_code") or "").lower())
    return f"{street}|{zipcode}"


def clean_locations(locations: list[dict], company_name: str) -> list[dict]:
    """Normalize, deduplicate, and quality-flag location records."""
    if not locations:
        return []

    cleaned = []
    seen = set()
    skipped_empty = 0
    skipped_dupe = 0

    for loc in locations:
        loc["company_name"] = company_name

        # Normalize fields
        loc["street_address"] = _normalize_whitespace(loc.get("street_address", "")).strip(",. ")
        loc["city"] = _title_case_safe(_normalize_whitespace(loc.get("city", "")).strip(",. "))
        loc["state"] = _normalize_state(loc.get("state", ""))
        loc["zip_code"] = _normalize_zip(loc.get("zip_code", ""))
        loc["country"] = _normalize_whitespace(loc.get("country", "")).upper()
        loc["phone_number"] = _normalize_phone(loc.get("phone_number", ""))
        loc["location_name"] = _normalize_whitespace(loc.get("location_name", ""))
        loc["hours_of_operation"] = _normalize_whitespace(loc.get("hours_of_operation", ""))
        loc["location_type"] = _normalize_whitespace(loc.get("location_type", ""))

        # Build full address
        loc["full_address"] = _build_full_address(loc)

        # Assess quality
        quality = _assess_quality(loc)
        if quality == "empty":
            skipped_empty += 1
            continue
        loc["data_quality"] = quality

        # Dedup
        key = _dedup_key(loc)
        if key and key != "|" and key in seen:
            skipped_dupe += 1
            continue
        if key and key != "|":
            seen.add(key)

        cleaned.append(loc)

    logger.info(
        "[%s] Cleaning: %d raw -> %d clean (%d empty, %d dupes removed)",
        company_name, len(locations), len(cleaned), skipped_empty, skipped_dupe,
    )
    return cleaned
