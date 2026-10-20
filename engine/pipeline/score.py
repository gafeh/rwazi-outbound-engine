import json
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from engine.llm import structured, FatalLLMError
from engine.config import MODEL_REASONING
from engine.db import conn

RUBRIC = """Score this lead 0-10 on each dimension. Weights are fixed.

1. market_fit (x3) - How poorly served is this market by traditional panels? \
Sub-Saharan Africa, frontier Asia and smaller LATAM markets score high. \
US, UK, Western Europe, Japan, Australia score low: Nielsen and Kantar already cover them.

2. buying_trigger (x3) - How urgent and recent is the data need implied by the signal? \
A new country manager with no local data infrastructure scores high. \
A routine backfill in an established market scores low. \
A posting with no expansion implication scores 0-2.

3. budget_proxy (x2) - Does this company plausibly fund a six-figure consumer \
intelligence contract? Global CPG, beverage, telco, pharma and large retail score high. \
Early-stage local startups, agencies and SMEs score low.

4. access (x2) - How reachable is a consumer insights or category decision maker? \
Companies with a visible insights function score high. Unnamed or agency-listed \
employers score 0-2.

Total = weighted sum, normalised to 0-100.
Be harsh. Most leads land between 35 and 60. Reserve 80+ for leads you would stake a \
quarter of pipeline on. If you find yourself scoring most leads above 70, you are being \
too generous."""

SYSTEM = f"""You score outbound leads for Rwazi, a zero-party consumer data platform \
serving global brands in Africa, Asia and LATAM. Competitors are GfK, Ipsos, Nielsen \
and Kantar: legacy panel providers who are slow or absent in these markets.

{RUBRIC}

Your rationale will be read by a human reviewer deciding whether to contact this lead. \
Ground every sub-score in concrete detail from the signal, not generic praise."""


class Score(BaseModel):
    market_fit: int = Field(ge=0, le=10)
    buying_trigger: int = Field(ge=0, le=10)
    budget_proxy: int = Field(ge=0, le=10)
    access: int = Field(ge=0, le=10)
    rationale: str = Field(
        description="3-5 sentences. The evidence behind each sub-score.")
    angle: str = Field(
        description="The single most compelling outreach hook, one sentence.")


WEIGHTS = {"market_fit": 3, "buying_trigger": 3, "budget_proxy": 2, "access": 2}


def _total(out: Score) -> int:
    raw = sum(getattr(out, k) * w for k, w in WEIGHTS.items())
    return round(raw / (10 * sum(WEIGHTS.values())) * 100)


def _score_one(r: dict):
    user = (f"Company: {r['company']} ({r['industry'] or 'unknown industry'})\n"
            f"Signal type: {r['triage_type']}\nMarket: {r['triage_market']}\n"
            f"Role: {r['title']} in {r['location']}\n"
            f"Triage note: {r['triage_reason']}\n"
            f"Excerpt:\n{(r['raw_excerpt'] or '')[:900]}")
    try:
        out = structured(MODEL_REASONING, SYSTEM, user, Score, max_tokens=3000)
    except FatalLLMError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"  scoring failed for signal {r['id']}: {e}")
        return None
    return r, out


def score_signals(limit: int = 40, workers: int = 6) -> dict:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT s.id, s.title, s.location, s.url, s.triage_type, s.triage_market,
                      s.triage_reason, s.raw_excerpt, co.name AS company, co.industry
               FROM signals s
               JOIN companies co ON co.id = s.company_id
               LEFT JOIN scores sc ON sc.signal_id = s.id
               WHERE s.triage_status='signal' AND sc.id IS NULL
               LIMIT ?""", (limit,)).fetchall()]

    if not rows:
        return {"scored": 0, "results": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = [x for x in pool.map(_score_one, rows) if x]

    results = []
    with conn() as c:
        for r, out in done:
            total = _total(out)
            breakdown = {k: getattr(out, k) for k in WEIGHTS}
            breakdown["angle"] = out.angle
            c.execute(
                """INSERT OR REPLACE INTO scores
                   (signal_id, total, breakdown_json, rationale, model)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["id"], total, json.dumps(breakdown), out.rationale, MODEL_REASONING),
            )
            results.append({"signal_id": r["id"], "company": r["company"], "total": total})

    totals = [x["total"] for x in results]
    return {"scored": len(results),
            "mean": round(sum(totals) / len(totals)) if totals else 0,
            "min": min(totals) if totals else 0,
            "max": max(totals) if totals else 0,
            "results": results}