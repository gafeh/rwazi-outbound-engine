import httpx

from engine.config import APOLLO_API_KEY, BUYER_TITLES
from engine.db import conn

SEARCH = "https://api.apollo.io/api/v1/mixed_people/search"


def _search(company: str, domain: str | None) -> list[dict]:
    payload = {
        "person_titles": BUYER_TITLES,
        "page": 1,
        "per_page": 3,
    }
    if domain:
        payload["q_organization_domains_list"] = [domain]
    else:
        payload["q_organization_name"] = company

    r = httpx.post(
        SEARCH,
        headers={"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("people", [])


def enrich_scored(min_score: int = 45, limit: int = 25) -> dict:
    if not APOLLO_API_KEY:
        return {"skipped": "no APOLLO_API_KEY set; drafts will address a role, "
                           "not a named person"}

    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT DISTINCT co.id, co.name, co.domain
               FROM companies co
               JOIN signals s ON s.company_id = co.id
               JOIN scores sc ON sc.signal_id = s.id
               LEFT JOIN contacts ct ON ct.company_id = co.id
               WHERE sc.total >= ? AND ct.id IS NULL
               LIMIT ?""", (min_score, limit)).fetchall()]

    found = 0
    for r in rows:
        try:
            people = _search(r["name"], r["domain"])
        except Exception as e:  # noqa: BLE001
            print(f"  apollo failed for {r['name']}: {e}")
            continue

        for p in people:
            email = p.get("email")
            # Apollo returns locked placeholders on the free tier. Never store them.
            if not email or "email_not_unlocked" in email or "@domain.com" in email:
                continue
            with conn() as c:
                c.execute(
                    """INSERT OR IGNORE INTO contacts
                       (company_id, full_name, title, email, linkedin_url,
                        source, confidence)
                       VALUES (?, ?, ?, ?, ?, 'apollo', ?)""",
                    (r["id"], p.get("name"), p.get("title"), email,
                     p.get("linkedin_url"), 0.8),
                )
            found += 1
            break  # one decision maker per company is enough

    return {"companies_checked": len(rows), "contacts_found": found}