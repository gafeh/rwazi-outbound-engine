import json

from pydantic import BaseModel, Field

from engine.llm import structured
from engine.config import MODEL_REASONING, SENDER_NAME, SENDER_COMPANY
from engine.db import conn

SYSTEM = f"""You write cold outbound email for {SENDER_COMPANY}, a zero-party consumer \
data platform giving brands real-time visibility into what people buy in Africa, Asia \
and LATAM, where traditional panels are stale or absent. Clients include large global \
consumer brands.

Rules, all mandatory:
- Open with the SPECIFIC signal you were given. Never "I saw you're in CPG."
- Under 110 words in the body. Short sentences. No adjective stacking.
- Exactly one ask, and it is low friction: a reply, not a 30-minute demo.
- No fabricated metrics, no invented case studies, no named clients you were not given.
- No "I hope this finds you well", "quick question", "circling back", "synergy", \
"leverage", "touch base", "pick your brain".
- No em dashes anywhere.
- Sign as {SENDER_NAME}.
- Plain text. Merge fields must be literal values, never placeholders like [Company].

In `claims`, list every factual statement you made about the prospect or their company. \
A separate reviewer will verify each one against source material. Omitting a claim is \
a failure."""


class Draft(BaseModel):
    subject: str = Field(description="Under 50 characters, lowercase, no clickbait.")
    body: str = Field(description="Under 110 words, plain text, signed.")
    claims: list[str] = Field(
        description="Every factual assertion made about the prospect or their company.")


def draft_for_scored(min_score: int = 60, limit: int = 25) -> dict:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT s.id AS signal_id, s.title, s.location, s.url, s.triage_market,
                      s.triage_type, sc.total, sc.breakdown_json, sc.rationale,
                      co.name AS company,
                      ct.id AS contact_id, ct.full_name, ct.title AS contact_title
               FROM signals s
               JOIN scores sc ON sc.signal_id = s.id
               JOIN companies co ON co.id = s.company_id
               LEFT JOIN contacts ct ON ct.company_id = co.id
               LEFT JOIN drafts d ON d.signal_id = s.id
               WHERE sc.total >= ? AND d.id IS NULL
               GROUP BY s.id
               ORDER BY sc.total DESC
               LIMIT ?""", (min_score, limit)).fetchall()]

    made = failed = 0
    for r in rows:
        angle = json.loads(r["breakdown_json"]).get("angle", "")
        user = (
            f"Prospect: {r['full_name'] or 'there'}, "
            f"{r['contact_title'] or 'Consumer Insights lead'} at {r['company']}\n"
            f"Signal: {r['company']} posted '{r['title']}' in {r['location']}\n"
            f"Signal URL: {r['url']}\n"
            f"Market: {r['triage_market']}\nSignal type: {r['triage_type']}\n"
            f"Lead score: {r['total']}/100\n"
            f"Recommended angle: {angle}\n"
            f"Scoring rationale (context only, do not quote):\n{r['rationale']}"
        )
        try:
            out = structured(MODEL_REASONING, SYSTEM, user, Draft, max_tokens=1200)
        except Exception as e:  # noqa: BLE001
            print(f"  drafting failed for signal {r['signal_id']}: {e}")
            failed += 1
            continue
        with conn() as c:
            c.execute(
                """INSERT INTO drafts
                   (signal_id, contact_id, step, subject, body, claims_json, model)
                   VALUES (?, ?, 1, ?, ?, ?, ?)""",
                (r["signal_id"], r["contact_id"], out.subject, out.body,
                 json.dumps(out.claims), MODEL_REASONING),
            )
        made += 1

    return {"drafted": made, "failed": failed}