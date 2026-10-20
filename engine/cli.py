import sys

import httpx
import yaml
from rich.console import Console
from rich.table import Table

from engine import db, enrich, llm
from engine.config import SEEDS, ADZUNA_COUNTRIES, ADZUNA_QUERIES
from engine.sources import greenhouse, lever, adzuna
from engine.pipeline import prefilter, classify, score, draft, qa
from engine.crm import hubspot

console = Console()


def _meter():
    s = llm.spend()
    if s["calls"]:
        console.print(f"[dim]spend: ${s['usd']} over {s['calls']} calls "
                      f"({s['in_tokens']} in / {s['out_tokens']} out)[/dim]")


# --- setup ----------------------------------------------------------------

def cmd_init():
    db.init()
    console.print("[green]schema ready[/green]")


def cmd_seed():
    data = yaml.safe_load((SEEDS / "companies.yaml").read_text()) or []
    for row in data:
        db.upsert_company(row["name"], row.get("domain"), row.get("ats_provider"),
                          row.get("ats_token"), row.get("industry"))
    console.print(f"[green]seeded {len(data)} companies[/green]")


def cmd_verify():
    """Ping every stored ATS token and report which boards are live."""
    rows = db.query(
        "SELECT name, ats_provider, ats_token FROM companies WHERE ats_token IS NOT NULL")
    if not rows:
        console.print("[yellow]no ATS tokens stored[/yellow]")
        return
    t = Table("company", "provider", "token", "status", "jobs")
    for r in rows:
        mod = greenhouse if r["ats_provider"] == "greenhouse" else lever
        try:
            jobs = mod.fetch(r["ats_token"])
            t.add_row(r["name"], r["ats_provider"], r["ats_token"],
                      "[green]live[/green]" if jobs else "[yellow]empty[/yellow]",
                      str(len(jobs)))
        except httpx.HTTPStatusError as e:
            t.add_row(r["name"], r["ats_provider"], r["ats_token"],
                      f"[red]{e.response.status_code}[/red]", "0")
        except Exception as e:  # noqa: BLE001
            t.add_row(r["name"], r["ats_provider"], r["ats_token"],
                      f"[red]{type(e).__name__}[/red]", "0")
    console.print(t)


def cmd_discover():
    """Bulk-test candidate ATS tokens; keep only those that resolve with jobs."""
    path = SEEDS / "ats_candidates.yaml"
    candidates = yaml.safe_load(path.read_text()) or []
    if not candidates:
        console.print(f"[yellow]{path} is empty, nothing to test[/yellow]")
        return
    t = Table("candidate", "provider", "status", "jobs")
    live = []
    for row in candidates:
        mod = greenhouse if row["ats_provider"] == "greenhouse" else lever
        try:
            jobs = mod.fetch(row["ats_token"])
            if jobs:
                t.add_row(row["name"], row["ats_provider"], "[green]live[/green]",
                          str(len(jobs)))
                live.append(row)
            else:
                t.add_row(row["name"], row["ats_provider"], "[yellow]empty[/yellow]", "0")
        except Exception as e:  # noqa: BLE001
            code = getattr(getattr(e, "response", None), "status_code", type(e).__name__)
            t.add_row(row["name"], row["ats_provider"], f"[red]{code}[/red]", "0")
    console.print(t)
    out = SEEDS / "companies.yaml"
    out.write_text(yaml.safe_dump(live, sort_keys=False))
    console.print(f"[green]wrote {len(live)} verified boards to {out}[/green]")


# --- pipeline -------------------------------------------------------------

def cmd_source():
    """Primary: Adzuna country-scoped search. Secondary: verified ATS boards."""
    total = new = calls = 0

    for country in ADZUNA_COUNTRIES:
        for what in ADZUNA_QUERIES:
            try:
                jobs = adzuna.fetch(country, what)
                calls += 1
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]adzuna {country}/{what}: {e}[/yellow]")
                continue
            for j in jobs:
                total += 1
                cid = db.upsert_company(j["company"])
                if db.insert_signal(cid, "adzuna", j["external_id"], j["title"],
                                    j["location"], j["url"], j["excerpt"]):
                    new += 1
            console.print(f"  {country}/{what}: {len(jobs)} results")

    rows = db.query(
        "SELECT id, name, ats_provider, ats_token FROM companies WHERE ats_token IS NOT NULL")
    for r in rows:
        mod = greenhouse if r["ats_provider"] == "greenhouse" else lever
        try:
            jobs = mod.fetch(r["ats_token"])
        except Exception:  # noqa: BLE001
            continue
        for j in jobs:
            total += 1
            if db.insert_signal(r["id"], r["ats_provider"], j["external_id"],
                                j["title"], j["location"], j["url"], j["excerpt"]):
                new += 1

    db.log_event("source", {"seen": total, "new": new, "adzuna_calls": calls})
    console.print(f"[green]{total} postings seen, {new} new, {calls} Adzuna calls[/green]")


def cmd_prefilter():
    """Free deterministic rejection. No API calls."""
    console.print(prefilter.run())


def cmd_triage():
    console.print(classify.triage_pending())
    _meter()


def cmd_score():
    out = score.score_signals()
    results = out.pop("results", [])
    if results:
        t = Table("signal", "company", "score")
        for r in sorted(results, key=lambda x: -x["total"]):
            t.add_row(str(r["signal_id"]), r["company"], str(r["total"]))
        console.print(t)
    console.print(out)
    _meter()


def cmd_enrich():
    console.print(enrich.enrich_scored())


def cmd_draft():
    console.print(draft.draft_for_scored())
    _meter()


def cmd_qa():
    console.print(qa.qa_drafts())
    _meter()


def cmd_revise():
    """One bounded revision pass over QA-failed drafts, then re-QA."""
    console.print(qa.revise_failed())
    console.print(qa.qa_drafts())
    _meter()


def cmd_sync():
    console.print(hubspot.sync_qa_passed())


# --- inspection -----------------------------------------------------------

def cmd_review():
    rows = db.query(
        """SELECT d.id, d.subject, d.body, d.status, d.step, q.passed, q.notes,
                  sc.total, co.name AS company, s.title, s.location
           FROM drafts d
           JOIN signals s ON s.id = d.signal_id
           JOIN companies co ON co.id = s.company_id
           JOIN scores sc ON sc.signal_id = s.id
           LEFT JOIN qa_results q ON q.id = (
               SELECT MAX(id) FROM qa_results WHERE draft_id = d.id)
           ORDER BY sc.total DESC""")
    if not rows:
        console.print("[yellow]no drafts yet, run draft first[/yellow]")
        return
    for r in rows:
        flag = "[green]PASS[/green]" if r["passed"] else "[red]FAIL[/red]"
        rev = " · revised" if r["step"] and r["step"] > 1 else ""
        console.rule(f"#{r['id']} {r['company']} · {r['total']}/100 · {flag}{rev}")
        console.print(f"[dim]{r['title']} in {r['location']}[/dim]")
        console.print(f"[bold]{r['subject']}[/bold]\n\n{r['body']}")
        if r["notes"]:
            console.print(f"[yellow]QA: {r['notes']}[/yellow]")


def cmd_funnel():
    """One-screen view of where volume is dying."""
    t = Table("stage", "count")
    for r in db.query(
            "SELECT triage_status, COUNT(*) n FROM signals GROUP BY triage_status"):
        t.add_row(r["triage_status"], str(r["n"]))
    for label, sql in [
        ("prefiltered_free",
         "SELECT COUNT(*) n FROM signals WHERE triage_reason LIKE 'prefilter:%'"),
        ("scored", "SELECT COUNT(*) n FROM scores"),
        ("contacts", "SELECT COUNT(*) n FROM contacts"),
        ("drafted", "SELECT COUNT(*) n FROM drafts"),
        ("revised", "SELECT COUNT(*) n FROM drafts WHERE step > 1"),
        ("qa_passed", "SELECT COUNT(*) n FROM drafts WHERE status='qa_passed'"),
        ("qa_failed", "SELECT COUNT(*) n FROM drafts WHERE status='qa_failed'"),
        ("crm_synced", "SELECT COUNT(*) n FROM drafts WHERE status='approved'"),
    ]:
        t.add_row(label, str(db.query(sql)[0]["n"]))
    console.print(t)


def cmd_dash():
    import uvicorn
    from engine.dashboard import app
    console.print("[green]dashboard on http://127.0.0.1:8000[/green]")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


# --- orchestration --------------------------------------------------------

def cmd_run():
    for fn in (cmd_source, cmd_prefilter, cmd_triage, cmd_score,
               cmd_enrich, cmd_draft, cmd_qa, cmd_revise, cmd_sync):
        console.rule(fn.__name__)
        fn()
    console.rule("funnel")
    cmd_funnel()
    _meter()


COMMANDS = {k[4:]: v for k, v in list(globals().items()) if k.startswith("cmd_")}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd not in COMMANDS:
        console.print(f"commands: {', '.join(sorted(COMMANDS))}")
        sys.exit(1)
    try:
        COMMANDS[cmd]()
    except llm.FatalLLMError as e:
        console.print(f"[red]{e}[/red]")
        _meter()
        sys.exit(2)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/yellow]")
        _meter()
        sys.exit(130)