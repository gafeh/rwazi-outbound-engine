from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from engine.llm import structured, FatalLLMError
from engine.config import MODEL_TRIAGE, TARGET_MARKETS
from engine.db import conn

SYSTEM = f"""You triage hiring signals for Rwazi, which sells real-time zero-party \
consumer data in markets where traditional research panels are stale or absent.

A posting is a SIGNAL only if it suggests the company is expanding into, or changing \
distribution within, one of these markets: {", ".join(TARGET_MARKETS)}.

Signal types:
- market_entry: a first or expanded commercial presence in a target market \
(country manager, GM, market lead, new-market commercial role)
- distribution: route-to-market, distributor, trade, or channel build-out in a target market
- insights_hire: a consumer insights, shopper insights, category or market research role \
covering a target market, meaning the data need is explicit
- other: anything else

Mark as NOISE:
- engineering, finance, HR, legal, IT and support roles
- any role located outside the target markets
- generic corporate roles with no market-expansion implication
- postings from recruitment agencies and staffing firms advertising on behalf of \
unnamed clients, since there is no identifiable buyer
- roles that merely contain a matching keyword (for example "category manager" in a \
retail store or a procurement function) with no expansion or consumer-data implication

Be strict. A false positive costs more than a missed lead. Most postings are noise."""


class Triage(BaseModel):
    is_signal: bool
    signal_type: Literal["market_entry", "distribution", "insights_hire", "other"]
    market: str = Field(description="Target market named, or empty string.")
    reason: str = Field(description="One sentence, max 25 words.")


def _triage_one(r: dict):
    user = (f"Company: {r['company']}\nTitle: {r['title']}\n"
            f"Location: {r['location']}\n\nExcerpt:\n{(r['raw_excerpt'] or '')[:600]}")
    try:
        out = structured(MODEL_TRIAGE, SYSTEM, user, Triage, max_tokens=2000)
    except FatalLLMError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"  triage failed for signal {r['id']}: {e}")
        return None
    return r["id"], ("signal" if out.is_signal else "noise"), out


def triage_pending(limit: int = 300, workers: int = 8) -> dict:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT s.id, s.title, s.location, s.raw_excerpt, co.name AS company
               FROM signals s
               JOIN companies co ON co.id = s.company_id
               WHERE s.triage_status = 'pending'
               LIMIT ?""", (limit,)).fetchall()]

    if not rows:
        return {"triaged": 0, "signals": 0, "noise": 0, "keep_rate": 0}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [x for x in pool.map(_triage_one, rows) if x]

    kept = dropped = 0
    with conn() as c:
        for sid, status, out in results:
            c.execute(
                """UPDATE signals
                   SET triage_status=?, triage_market=?, triage_type=?, triage_reason=?
                   WHERE id=?""",
                (status, out.market, out.signal_type, out.reason, sid),
            )
            kept += status == "signal"
            dropped += status == "noise"

    return {"triaged": len(results), "signals": kept, "noise": dropped,
            "keep_rate": round(kept / len(results) * 100) if results else 0}