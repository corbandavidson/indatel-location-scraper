"""
Shared location cache backed by Supabase (PostgREST).

When configured, the app checks the database before scraping.  If another
user already scraped the same company recently, results come back instantly.
After a fresh scrape the results are pushed so every other user benefits.

No extra Python packages — uses `requests` which is already a dependency.
"""

import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger("scraper_ai.shared_cache")

_LOCATION_FIELDS = [
    "company_name", "location_name", "street_address", "city", "state",
    "zip_code", "country", "full_address", "phone_number",
    "hours_of_operation", "location_type", "latitude", "longitude",
    "location_url", "source_url", "data_quality",
]


class SharedCache:
    def __init__(self, supabase_url: str, supabase_key: str,
                 max_age_days: int = 30):
        self.base = supabase_url.rstrip("/")
        self.headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        }
        self.max_age_days = max_age_days

    def get_cached(self, company_name: str) -> tuple[list[dict], int] | None:
        """Return (locations, age_in_days) or None if not cached / expired."""
        try:
            r = requests.get(
                f"{self.base}/rest/v1/scrape_cache",
                headers=self.headers,
                params={
                    "company_name": f"eq.{company_name}",
                    "select": "scraped_at,location_count",
                    "limit": "1",
                },
                timeout=5,
            )
            if r.status_code != 200 or not r.json():
                return None

            entry = r.json()[0]
            scraped_at = datetime.fromisoformat(entry["scraped_at"])
            if scraped_at.tzinfo is None:
                scraped_at = scraped_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - scraped_at).days
            if age > self.max_age_days:
                return None

            r = requests.get(
                f"{self.base}/rest/v1/locations",
                headers=self.headers,
                params={
                    "company_name": f"eq.{company_name}",
                    "limit": "50000",
                },
                timeout=15,
            )
            if r.status_code != 200:
                return None
            locs = r.json()
            if not locs:
                return None

            logger.info("[%s] Cache hit: %d locations (scraped %d day(s) ago)",
                        company_name, len(locs), age)
            return locs, age
        except Exception as e:
            logger.debug("Cache lookup failed: %s", e)
            return None

    def save(self, company_name: str, locations: list[dict],
             scraped_by: str = "") -> None:
        if not locations:
            return
        try:
            requests.post(
                f"{self.base}/rest/v1/scrape_cache",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates"},
                json={
                    "company_name": company_name,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "location_count": len(locations),
                    "scraped_by": scraped_by,
                },
                timeout=5,
            )

            requests.delete(
                f"{self.base}/rest/v1/locations",
                headers=self.headers,
                params={"company_name": f"eq.{company_name}"},
                timeout=10,
            )

            batch_size = 500
            for i in range(0, len(locations), batch_size):
                batch = [
                    {k: loc.get(k, "") for k in _LOCATION_FIELDS}
                    for loc in locations[i:i + batch_size]
                ]
                requests.post(
                    f"{self.base}/rest/v1/locations",
                    headers=self.headers,
                    json=batch,
                    timeout=15,
                )

            logger.info("[%s] Saved %d locations to shared cache",
                        company_name, len(locations))
        except Exception as e:
            logger.warning("Failed to save to shared cache: %s", e)

    def list_companies(self) -> list[dict]:
        try:
            r = requests.get(
                f"{self.base}/rest/v1/scrape_cache",
                headers=self.headers,
                params={
                    "select": "company_name,scraped_at,location_count,scraped_by",
                    "order": "scraped_at.desc",
                    "limit": "500",
                },
                timeout=10,
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []
