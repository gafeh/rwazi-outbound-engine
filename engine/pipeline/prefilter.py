import re
from engine.db import conn
from engine.config import SIGNAL_TITLE_HINTS, TARGET_MARKETS

AGENCY = re.compile(
    r"recruit|staffing|talent|placement|manpower|headhunt|consultanc|"
    r"hr solutions|personnel|resourcing|employment agency|confidential",
    re.I)

TITLE_HINT = re.compile("|".join(re.escape(h) for h in SIGNAL_TITLE_HINTS), re.I)
MARKET_HINT = re.compile("|".join(re.escape(m) for m in TARGET_MARKETS), re.I)

HARD_NO = re.compile(
    r"\b(software|developer|engineer|nurse|driver|teacher|accountant|"
    r"security guard|cleaner|intern|warehouse|cashier|call cent|"
    r"receptionist|technician|welder|electrician)\b", re.I)


def reason_to_drop(title: str, company: str, excerpt: str) -> str | None:
    if AGENCY.search(company or ""):
        return "agency or unnamed employer"
    if HARD_NO.search(title or ""):
        return "role category out of scope"
    if not TITLE_HINT.search(title or ""):
        return "no expansion or insights signal in title"
    if not MARKET_HINT.search(f"{title} {excerpt}"[:1500]):
        return "no target market referenced"
    return None


def run() -> dict:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT s.id, s.title, s.raw_excerpt, co.name AS company
               FROM signals s JOIN companies co ON co.id = s.company_id
               WHERE s.triage_status = 'pending'""").fetchall()]

    dropped = 0
    with conn() as c:
        for r in rows:
            why = reason_to_drop(r["title"], r["company"], r["raw_excerpt"] or "")
            if why:
                c.execute(
                    """UPDATE signals SET triage_status='noise', triage_type='other',
                       triage_reason=? WHERE id=?""", (f"prefilter: {why}", r["id"]))
                dropped += 1

    return {"examined": len(rows), "dropped_free": dropped,
            "sent_to_llm": len(rows) - dropped,
            "saved_pct": round(dropped / len(rows) * 100) if rows else 0}