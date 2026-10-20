import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from engine.llm import structured, FatalLLMError
from engine.config import MODEL_REASONING, SENDER_NAME
from engine.db import conn

BANNED = ["hope this finds you well", "quick question", "circling back", "synergy",
          "leverage", "game-changer", "revolutionary", "i wanted to reach out",
          "touch base", "pick your brain"]
PLACEHOLDER = re.compile(r"\[[A-Za-z _]+\]|\{\{.*?\}\}|XYZ|<[a-z_]+>")


def deterministic_checks(subject: str, body: str) -> dict:
    words = len(body.split())
    low = body.lower()
    return {
        "word_count_ok": words <= 125,
        "subject_length_ok": 0 < len(subject) <= 55,
        "no_placeholders": not bool(PLACEHOLDER.search(body + subject)),
        "no_em_dash": "—" not in body and "—" not in subject,
        "no_banned_phrases": not any(p in low for p in BANNED),
        "signed": SENDER_NAME.split()[0].lower() in low,
        "single_ask": body.count("?") <= 2,
        "_word_count": words,
    }


SYSTEM = """You are the QA gate for outbound email. You did not write this draft and \
you have no stake in it passing. Your job is to catch what would embarrass the sender.

SCOPE OF CLAIM CHECKING
Only claims about THIS PROSPECT or THEIR COMPANY are in scope. A claim is UNSUPPORTED \
if the source posting does not establish it. Examples of unsupported claims: asserting \
the company lacks data today, naming markets the posting does not name, assuming the \
recipient's job function, inventing metrics, client names or timelines.

OUT OF SCOPE. Never fail a draft for any of these:
- General statements about the market research industry, or about panel coverage gaps \
in emerging markets. That is the sender's own positioning, not a claim about the prospect.
- A single sentence describing what the sender's product does.
- A generic greeting when no contact name was available. The input states whether a \
name was supplied. If it was not, "Hi there" is correct and is not a defect.

TONE
Fail on vendor-brochure voice: a product paragraph that could appear in any email, \
filler like "hires like that tend to", or a CTA whose only ask is permission to send \
more material. Pass a note that makes one specific observation drawn from the posting \
and asks one concrete question.

Fail only on an in-scope unsupported claim or on tone. Be strict, but do not invent \
faults outside the scope above."""


class QAResult(BaseModel):
    claims_supported: bool
    unsupported_claims: list[str]
    tone_ok: bool
    tone_note: str = Field(description="One sentence.")
    verdict: Literal["pass", "fail"]
    fix_hint: str = Field(
        description="If fail, the single most important change. Otherwise empty string.")


def qa_drafts(limit: int = 25) -> dict:
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT d.id, d.subject, d.body, d.claims_json, d.step,
                      s.title, s.location, s.url, s.raw_excerpt, s.triage_market,
                      co.name AS company, ct.full_name
               FROM drafts d
               JOIN signals s ON s.id = d.signal_id
               JOIN companies co ON co.id = s.company_id
               LEFT JOIN contacts ct ON ct.id = d.contact_id
               WHERE d.status = 'drafted'
               LIMIT ?""", (limit,)).fetchall()]

    passed = failed = 0
    for r in rows:
        det = deterministic_checks(r["subject"], r["body"])
        det_ok = all(v for k, v in det.items() if not k.startswith("_"))

        claims = json.loads(r["claims_json"] or "[]")
        source = (f"Company: {r['company']}\nPosting: {r['title']} in {r['location']}\n"
                  f"URL: {r['url']}\nMarket: {r['triage_market']}\n"
                  f"Posting text:\n{(r['raw_excerpt'] or '')[:900]}")
        name_line = (f"yes, {r['full_name']}" if r["full_name"] else
                     "no, enrichment returned no contact for this company")
        user = (f"CONTACT NAME AVAILABLE: {name_line}\n\n"
                f"SOURCE SIGNAL:\n{source}\n\n"
                f"DRAFT SUBJECT: {r['subject']}\nDRAFT BODY:\n{r['body']}\n\n"
                f"CLAIMS MADE:\n" + "\n".join(f"- {c}" for c in claims))

        try:
            out = structured(MODEL_REASONING, SYSTEM, user, QAResult, max_tokens=3000)
        except FatalLLMError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  QA failed for draft {r['id']}: {e}")
            continue

        ok = det_ok and out.verdict == "pass"
        checks = {**{k: v for k, v in det.items() if not k.startswith("_")},
                  "word_count": det["_word_count"],
                  "claims_supported": out.claims_supported,
                  "unsupported_claims": out.unsupported_claims,
                  "tone_ok": out.tone_ok,
                  "step": r["step"]}
        notes = out.tone_note + ((" FIX: " + out.fix_hint) if out.fix_hint else "")

        with conn() as c:
            c.execute(
                """INSERT INTO qa_results (draft_id, passed, checks_json, notes, model)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["id"], int(ok), json.dumps(checks), notes, MODEL_REASONING))
            c.execute("UPDATE drafts SET status=? WHERE id=?",
                      ("qa_passed" if ok else "qa_failed", r["id"]))
        passed += ok
        failed += not ok

    return {"checked": len(rows), "passed": passed, "failed": failed,
            "pass_rate": round(passed / len(rows) * 100) if rows else 0}


def revise_failed(limit: int = 15) -> dict:
    """Feed each QA fix_hint back to the drafter exactly once.

    The `step = 1` filter is the loop guard: a draft gets one revision, then it
    either passes or stays dead. An unbounded draft/QA loop burns money and
    converges on nothing.
    """
    from engine.pipeline.draft import Draft, SYSTEM as DRAFT_SYSTEM

    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT d.id, d.subject, d.body, d.signal_id,
                      q.notes, s.title, s.location, s.raw_excerpt,
                      co.name AS company, ct.full_name
               FROM drafts d
               JOIN qa_results q ON q.draft_id = d.id
               JOIN signals s ON s.id = d.signal_id
               JOIN companies co ON co.id = s.company_id
               LEFT JOIN contacts ct ON ct.id = d.contact_id
               WHERE d.status = 'qa_failed' AND d.step = 1
               GROUP BY d.id
               LIMIT ?""", (limit,)).fetchall()]

    revised = 0
    for r in rows:
        greeting = (f"Address them as {r['full_name']}." if r["full_name"]
                    else "No contact name is available, so a role-level greeting "
                         "is correct. Do not invent a name.")
        user = (f"Your previous draft was rejected by an independent reviewer.\n\n"
                f"SIGNAL: {r['company']} posted '{r['title']}' in {r['location']}\n"
                f"POSTING:\n{(r['raw_excerpt'] or '')[:700]}\n\n"
                f"{greeting}\n\n"
                f"PREVIOUS SUBJECT: {r['subject']}\n"
                f"PREVIOUS BODY:\n{r['body']}\n\n"
                f"REVIEWER FEEDBACK:\n{r['notes']}\n\n"
                f"Rewrite it. Make the specific change the reviewer asked for rather "
                f"than rewording around it. If the reviewer called the product "
                f"paragraph brochure-like, cut it to one plain sentence or remove it. "
                f"If the reviewer called the ask weak, replace it with one concrete "
                f"question the recipient can answer in a line.")
        try:
            out = structured(MODEL_REASONING, DRAFT_SYSTEM, user, Draft, max_tokens=3000)
        except FatalLLMError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  revision failed for draft {r['id']}: {e}")
            continue

        with conn() as c:
            c.execute("""UPDATE drafts SET subject=?, body=?, claims_json=?,
                         step=2, status='drafted' WHERE id=?""",
                      (out.subject, out.body, json.dumps(out.claims), r["id"]))
        revised += 1

    return {"revised": revised}