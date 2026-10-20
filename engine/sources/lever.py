# engine/sources/lever.py
import httpx

BASE = "https://api.lever.co/v0/postings/{token}?mode=json"


def fetch(token: str) -> list[dict]:
    r = httpx.get(BASE.format(token=token), timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json():
        out.append({
            "external_id": f"lv:{token}:{j['id']}",
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "excerpt": (j.get("descriptionPlain") or "")[:1200],
        })
    return out