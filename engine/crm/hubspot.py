import json
import time

import httpx

from engine.config import HUBSPOT_TOKEN
from engine.db import conn

BASE = "https://api.hubapi.com"


def _headers() -> dict:
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_SERVICE_KEY not set in .env")
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}",
            "Content-Type": "application/json"}


def _req(method: str, path: str, **kw) -> dict:
    """Thin wrapper with a retry on 429. Free tier allows 100 req / 10 sec."""
    for attempt in range(4):
        r = httpx.request(method, f"{BASE}{path}", headers=_headers(), timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json() if r.content else {}
    raise RuntimeError(f"hubspot rate limited on {path}")


def _search(obj: str, prop: str, value: str) -> str | None:
    body = {"filterGroups": [{"filters": [
        {"propertyName": prop, "operator": "EQ", "value": value}]}],
        "limit": 1}
    res = _req("POST", f"/crm/v3/objects/{obj}/search", json=body)
    results = res.get("results", [])
    return results[0]["id"] if results else None


def upsert_company(name: str, domain: str | None, industry: str | None) -> str:
    existing = _search("companies", "name", name)
    props = {"name": name}
    if domain:
        props["domain"] = domain
    if industry:
        props["description"] = f"Industry: {industry}"
    if existing:
        _req("PATCH", f"/crm/v3/objects/companies/{existing}", json={"properties": props})
        return existing
    return _req("POST", "/crm/v3/objects/companies", json={"properties": props})["id"]


def upsert_contact(email: str, full_name: str | None, title: str | None) -> str:
    existing = _search("contacts", "email", email)
    first, _, last = (full_name or "").partition(" ")
    props = {"email": email}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if title:
        props["jobtitle"] = title
    if existing:
        _req("PATCH", f"/crm/v3/objects/contacts/{existing}", json={"properties": props})
        return existing
    return _req("POST", "/crm/v3/objects/contacts", json={"properties": props})["id"]


def create_deal(name: str, description: str, stage: str = "appointmentscheduled") -> str:
    props = {"dealname": name, "pipeline": "default", "dealstage": stage,
             "description": description[:65000]}
    return _req("POST", "/crm/v3/objects/deals", json={"properties": props})["id"]


def associate(from_obj: str, from_id: str, to_obj: str, to_id: str) -> None:
    """Association v4 default labels. Idempotent on repeat calls."""
    _req("PUT",
         f"/crm/v4/objects/{from_obj}/{from_id}/associations/default/{to_obj}/{to_id}")


def sync_qa_passed(limit: int = 25) -> dict:
    """Push every QA-passed draft into HubSpot as company + contact + deal."""
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT d.id AS draft_id, d.subject, d.body,
                      s.title, s.location, s.url, s.triage_market, s.triage_type,
                      sc.total, sc.rationale, sc.breakdown_json,
                      co.name AS company, co.domain, co.industry,
                      ct.email, ct.full_name, ct.title AS contact_title
               FROM drafts d
               JOIN signals s ON s.id = d.signal_id
               JOIN scores sc ON sc.signal_id = s.id
               JOIN companies co ON co.id = s.company_id
               LEFT JOIN contacts ct ON ct.id = d.contact_id
               WHERE d.status = 'qa_passed'
               LIMIT ?""", (limit,)).fetchall()]

    synced = failed = 0
    for r in rows:
        try:
            company_id = upsert_company(r["company"], r["domain"], r["industry"])

            contact_id = None
            if r["email"]:
                contact_id = upsert_contact(r["email"], r["full_name"],
                                            r["contact_title"])
                associate("contacts", contact_id, "companies", company_id)

            angle = json.loads(r["breakdown_json"]).get("angle", "")
            description = (
                f"LEAD SCORE: {r['total']}/100\n"
                f"SIGNAL: {r['title']} in {r['location']} ({r['triage_type']})\n"
                f"MARKET: {r['triage_market']}\n"
                f"SOURCE: {r['url']}\n\n"
                f"ANGLE: {angle}\n\n"
                f"SCORING RATIONALE:\n{r['rationale']}\n\n"
                f"DRAFTED OUTREACH (QA passed)\n"
                f"Subject: {r['subject']}\n\n{r['body']}"
            )
            deal_id = create_deal(
                f"{r['company']} — {r['triage_market'] or 'expansion'} signal",
                description)
            associate("deals", deal_id, "companies", company_id)
            if contact_id:
                associate("deals", deal_id, "contacts", contact_id)

            with conn() as c:
                c.execute("UPDATE drafts SET status='approved' WHERE id=?",
                          (r["draft_id"],))
            synced += 1
        except Exception as e:  # noqa: BLE001
            print(f"  hubspot sync failed for draft {r['draft_id']}: {e}")
            failed += 1

    return {"synced": synced, "failed": failed}