import time
import httpx
from engine.config import ADZUNA_APP_ID, ADZUNA_APP_KEY

BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_last_call = [0.0]


def _throttle(min_gap: float = 2.5):
    """Free tier allows 25 calls/minute. Stay well under."""
    gap = time.monotonic() - _last_call[0]
    if gap < min_gap:
        time.sleep(min_gap - gap)
    _last_call[0] = time.monotonic()


def fetch(country: str, what: str, page: int = 1, per_page: int = 50) -> list[dict]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env")
    _throttle()
    r = httpx.get(
        BASE.format(country=country, page=page),
        params={
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "results_per_page": per_page,
            "what": what,
            "content-type": "application/json",
        },
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("results", []):
        out.append({
            "external_id": f"az:{country}:{j.get('id')}",
            "company": (j.get("company") or {}).get("display_name") or "Unknown",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "url": j.get("redirect_url", ""),
            "excerpt": (j.get("description") or "")[:1200],
            "country": country,
        })
    return out