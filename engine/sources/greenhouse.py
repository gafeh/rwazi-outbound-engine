# engine/sources/greenhouse.py
import httpx

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def fetch(token: str) -> list[dict]:
    r = httpx.get(BASE.format(token=token), timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "external_id": f"gh:{token}:{j['id']}",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "excerpt": (j.get("content") or "")[:1200],
        })
    return out