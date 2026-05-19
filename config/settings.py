import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
LOG_DIR = BASE_DIR / "logs"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
BROWSER = os.getenv("BROWSER", "chromium")
MAX_LOCATIONS = int(os.getenv("MAX_LOCATIONS_PER_COMPANY", "0"))

REQUEST_DELAY_MIN = 1.5
REQUEST_DELAY_MAX = 4.0
LONG_DELAY_MIN = 8.0
LONG_DELAY_MAX = 12.0
LONG_DELAY_EVERY_N = 10
BACKOFF_INITIAL = 30
BACKOFF_MAX = 120

VIEWPORT = {"width": 1280, "height": 800}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/109.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

LOCATOR_URL_PATTERNS = [
    # Subdomain patterns first — Yext / Uberall hosts complete listings
    # here, and they almost never geo-filter.
    "https://locations.{slug}.com",
    "https://stores.{slug}.com",
    "https://restaurants.{slug}.com",
    "https://locations.{slug}.us",
    "https://stores.{slug}.us",
    # Non-www (common for single-product brands)
    "https://{slug}.com/locations",
    "https://{slug}.com/store-locator",
    "https://{slug}.com/store-finder",
    # Search-interface paths — these pages load the real locator API on
    # user interaction.
    "https://www.{slug}.com/store-locator",
    "https://www.{slug}.com/store-finder",
    "https://www.{slug}.com/find-a-store",
    "https://www.{slug}.com/find-a-location",
    "https://www.{slug}.com/storelocator",
    "https://www.{slug}.com/storefinder",
    "https://www.{slug}.com/find-store",
    "https://www.{slug}.com/find-location",
    # Listing paths — usually static or paginated
    "https://www.{slug}.com/locations",
    "https://www.{slug}.com/locations/all",
    "https://www.{slug}.com/store-directory",
    "https://www.{slug}.com/restaurants",
    "https://www.{slug}.com/branches",
    "https://www.{slug}.com/stores",
    "https://www.{slug}.com/store",
    "https://www.{slug}.com/our-locations",
    "https://www.{slug}.com/our-stores",
    "https://www.{slug}.com/find-us",
    "https://www.{slug}.com/visit-us",
    # Non-retail networks (co-ops, member orgs, B2B)
    "https://www.{slug}.com/members",
    "https://www.{slug}.com/our-members",
    "https://www.{slug}.com/network",
    "https://www.{slug}.com/network-map",
    "https://www.{slug}.com/coverage",
    "https://www.{slug}.com/service-area",
    "https://www.{slug}.com/dealers",
    "https://www.{slug}.com/dealer-locator",
]

JS_RENDER_MARKERS = [
    "window.__INITIAL_STATE__",
    "window.__NEXT_DATA__",
    "window.__NUXT__",
    "window.PAGE_DATA",
    "react-root",
    "app-root",
    "__next",
    "id=\"__nuxt\"",
]

LOCATION_JSON_KEYS = [
    "address", "street", "streetAddress", "street_address",
    "city", "state", "zip", "zipCode", "zip_code", "postalCode", "postal_code",
    "lat", "lng", "latitude", "longitude",
    "phone", "phoneNumber", "phone_number",
    "storeNumber", "store_number", "locationName", "location_name",
]

API_ENDPOINT_PATTERNS = [
    r"/api/locations",
    r"/api/stores",
    r"/api/v\d+/locations",
    r"/api/v\d+/stores",
    r"/stores\.json",
    r"/locations\.json",
    r"/store-directory",
    r"/rest/model/atg/store",
    r"/bff/locations",
    r"/graphql",
]
