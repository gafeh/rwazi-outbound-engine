from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from engine.db import query

app = FastAPI(title="Rwazi Outbound Engine")

# Per-lead minutes. The manual figure is modeled, not observed, and is labeled
# as modeled everywhere it surfaces.
MIN_PER_LEAD_MANUAL = 12   # SDR sourcing + research + personalization
MIN_PER_LEAD_ENGINE = 1.5  # human review of a QA-passed draft


def _one(sql, params=()):
    return query(sql, params)[0]["n"]


def metrics() -> dict:
    total = _one("SELECT COUNT(*) n FROM signals")
    noise = _one("SELECT COUNT(*) n FROM signals WHERE triage_status='noise'")
    prefiltered = _one(
        "SELECT COUNT(*) n FROM signals WHERE triage_reason LIKE 'prefilter:%'")
    signals = _one("SELECT COUNT(*) n FROM signals WHERE triage_status='signal'")
    pending = _one("SELECT COUNT(*) n FROM signals WHERE triage_status='pending'")
    scored = _one("SELECT COUNT(*) n FROM scores")
    contacts = _one("SELECT COUNT(*) n FROM contacts")
    drafted = _one("SELECT COUNT(*) n FROM drafts")
    revised = _one("SELECT COUNT(*) n FROM drafts WHERE step > 1")
    qa_pass = _one(
        "SELECT COUNT(*) n FROM drafts WHERE status IN ('qa_passed','approved')")
    qa_fail = _one("SELECT COUNT(*) n FROM drafts WHERE status='qa_failed'")
    synced = _one("SELECT COUNT(*) n FROM drafts WHERE status='approved'")

    # Every rejection ever recorded, not just currently-failing drafts. A draft
    # that was blocked, revised and then passed is the gate working, and that
    # history is the evidence it works.
    blocked_ever = _one("SELECT COUNT(*) n FROM qa_results WHERE passed = 0")

    # Keep rate is the MODEL's judgement only. Prefilter rejections never reached
    # a model, so including them in the denominator mixes two different filters.
    model_triaged = _one(
        """SELECT COUNT(*) n FROM signals
           WHERE triage_status IN ('signal','noise')
             AND (triage_reason IS NULL OR triage_reason NOT LIKE 'prefilter:%')""")

    dist = query(
        """SELECT CASE
                    WHEN total >= 70 THEN '70+'
                    WHEN total >= 55 THEN '55-69'
                    WHEN total >= 40 THEN '40-54'
                    ELSE 'Under 40' END AS band,
                  COUNT(*) n
           FROM scores GROUP BY band""")
    order = {"70+": 0, "55-69": 1, "40-54": 2, "Under 40": 3}
    dist.sort(key=lambda d: order.get(d["band"], 9))

    top = query(
        """SELECT co.name company, sc.total, sc.breakdown_json,
                  s.title, s.location, s.triage_market
           FROM scores sc
           JOIN signals s ON s.id = sc.signal_id
           JOIN companies co ON co.id = s.company_id
           ORDER BY sc.total DESC LIMIT 8""")

    markets = query(
        """SELECT COALESCE(NULLIF(s.triage_market,''),'Unspecified') m, COUNT(*) n
           FROM signals s JOIN scores sc ON sc.signal_id = s.id
           GROUP BY m ORDER BY n DESC LIMIT 6""")

    fails = query(
        """SELECT q.notes, q.checked_at, co.name company, d.step, d.status
           FROM qa_results q
           JOIN drafts d ON d.id = q.draft_id
           JOIN signals s ON s.id = d.signal_id
           JOIN companies co ON co.id = s.company_id
           WHERE q.passed = 0
           ORDER BY q.id DESC LIMIT 4""")

    # Baseline counts only leads that reached a reviewable state. Counting every
    # scanned posting would claim credit for research no human would have done.
    manual_min = qa_pass * MIN_PER_LEAD_MANUAL
    engine_min = qa_pass * MIN_PER_LEAD_ENGINE

    return {
        "sourced": total, "prefiltered": prefiltered, "noise": noise,
        "signals": signals, "pending": pending, "scored": scored,
        "contacts": contacts, "drafted": drafted, "revised": revised,
        "qa_pass": qa_pass, "qa_fail": qa_fail, "synced": synced,
        "blocked_ever": blocked_ever, "model_triaged": model_triaged,
        "qa_pass_rate": round(qa_pass / drafted * 100) if drafted else 0,
        "keep_rate": round(signals / model_triaged * 100) if model_triaged else 0,
        "free_reject_rate": round(prefiltered / total * 100) if total else 0,
        "distribution": dist, "top": top, "markets": markets, "fails": fails,
        "hours_manual": round(manual_min / 60, 1),
        "hours_engine": round(engine_min / 60, 1),
        "hours_saved": round((manual_min - engine_min) / 60, 1),
    }


@app.get("/api/metrics")
def api_metrics():
    return metrics()


# --- rendering ------------------------------------------------------------

NAV = [("Overview", True), ("Pipeline", False), ("Leads", False),
       ("QA gate", False), ("Sources", False), ("Settings", False)]


def _kpi(label, value, tag, tag_good, sub):
    arrow = "&#9650;" if tag_good else "&#9660;"
    cls = "up" if tag_good else "down"
    return f"""
    <div class="kpi">
      <div class="kpi-top"><span class="kpi-label">{label}</span></div>
      <div class="kpi-row">
        <span class="kpi-value">{value}</span>
        <span class="pill {cls}">{arrow} {tag}</span>
      </div>
      <div class="kpi-sub">{sub}</div>
    </div>"""


def _funnel_rows(m):
    stages = [("Sourced", m["sourced"], "#3b6ef5"),
              ("Rejected free", m["prefiltered"], "#94a3b8"),
              ("Model triaged", m["model_triaged"], "#64748b"),
              ("Qualified", m["signals"], "#10b981"),
              ("Scored", m["scored"], "#f59e0b"),
              ("Drafted", m["drafted"], "#8b5cf6"),
              ("QA passed", m["qa_pass"], "#06b6d4"),
              ("In CRM", m["synced"], "#ec4899")]
    top = max((v for _, v, _ in stages), default=1) or 1
    out = []
    for name, val, color in stages:
        pct = max(val / top * 100, 1.2 if val else 0)
        out.append(f"""
        <div class="frow">
          <div class="fname">{name}</div>
          <div class="ftrack"><div class="fbar" style="width:{pct}%;
               background:{color}"></div></div>
          <div class="fval">{val}</div>
        </div>""")
    return "".join(out)


def _donut(pct, revised):
    r, c = 54, 2 * 3.14159 * 54
    filled = c * pct / 100
    color = "#10b981" if pct >= 50 else "#f59e0b" if pct > 0 else "#ef4444"
    cap = f"after {revised} revised" if revised else "QA pass"
    return f"""
    <svg viewBox="0 0 140 140" class="donut">
      <circle cx="70" cy="70" r="{r}" fill="none" stroke="var(--track)"
              stroke-width="14"/>
      <circle cx="70" cy="70" r="{r}" fill="none" stroke="{color}"
              stroke-width="14" stroke-linecap="round"
              stroke-dasharray="{filled:.1f} {c - filled:.1f}"
              transform="rotate(-90 70 70)"/>
      <text x="70" y="66" class="d-val">{pct}%</text>
      <text x="70" y="86" class="d-cap">{cap}</text>
    </svg>"""


def _dist_bars(dist):
    if not dist:
        return '<div class="empty">No scored leads yet</div>'
    top = max(d["n"] for d in dist) or 1
    colors = {"70+": "#10b981", "55-69": "#3b6ef5",
              "40-54": "#f59e0b", "Under 40": "#cbd5e1"}
    bars = []
    for d in dist:
        h = max(d["n"] / top * 100, 4)
        bars.append(f"""
        <div class="col">
          <div class="colnum">{d['n']}</div>
          <div class="colbar" style="height:{h}%;
               background:{colors.get(d['band'], '#cbd5e1')}"></div>
          <div class="collab">{d['band']}</div>
        </div>""")
    return f'<div class="cols">{"".join(bars)}</div>'


def _top_rows(top):
    if not top:
        return '<tr><td colspan="4" class="empty">No scored leads yet</td></tr>'
    import json
    out = []
    for t in top:
        try:
            angle = json.loads(t["breakdown_json"]).get("angle", "")
        except Exception:  # noqa: BLE001
            angle = ""
        tone = "hot" if t["total"] >= 65 else "warm" if t["total"] >= 50 else "cool"
        initial = (t["company"] or "?")[0].upper()
        out.append(f"""
        <tr>
          <td><div class="co"><span class="av">{initial}</span>
              <div><div class="co-name">{t['company']}</div>
              <div class="co-sub">{t['location'] or ''}</div></div></div></td>
          <td><span class="score {tone}">{t['total']}</span></td>
          <td class="sig">{t['title']}</td>
          <td class="angle">{angle[:90]}</td>
        </tr>""")
    return "".join(out)


def _market_rows(markets):
    if not markets:
        return '<div class="empty">Nothing scored yet</div>'
    top = max(x["n"] for x in markets) or 1
    return "".join(f"""
      <div class="mrow">
        <span class="mname">{x['m']}</span>
        <div class="mtrack"><div class="mbar" style="width:{x['n']/top*100:.0f}%"></div></div>
        <span class="mval">{x['n']}</span>
      </div>""" for x in markets)


def _fail_rows(fails):
    if not fails:
        return ('<div class="empty">No rejections recorded. A gate nothing fails '
                'is not doing work.</div>')
    out = []
    for f in fails:
        fixed = f["status"] in ("qa_passed", "approved")
        badge = (f'<span class="rev ok">fixed on revision</span>' if fixed
                 else f'<span class="rev bad">still blocked</span>')
        out.append(f"""
      <div class="fail{' fixed' if fixed else ''}">
        <div class="fail-co">{f['company']}{badge}</div>
        <div class="fail-note">{(f['notes'] or '')[:150]}</div>
      </div>""")
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def index():
    m = metrics()

    nav = "".join(
        f'<a class="nav{" on" if active else ""}" href="#">'
        f'<span class="dot"></span>{label}</a>' for label, active in NAV)

    kpis = "".join([
        _kpi("Postings sourced", f'{m["sourced"]:,}',
             f'{m["free_reject_rate"]}% free reject', True,
             f'{m["pending"]:,} still queued'),
        _kpi("Qualified signals", m["signals"], f'{m["keep_rate"]}% keep rate', True,
             f'of {m["model_triaged"]:,} seen by the model'),
        _kpi("Drafts QA passed", m["qa_pass"], f'{m["qa_pass_rate"]}% pass',
             m["qa_pass_rate"] >= 50,
             f'{m["blocked_ever"]} rejected first, {m["revised"]} repaired'),
        _kpi("Hours saved", m["hours_saved"], "modeled", True,
             f'{m["hours_manual"]}h manual vs {m["hours_engine"]}h review, '
             f'on {m["qa_pass"]} reviewable leads'),
    ])

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outbound Engine</title>
<style>
:root {{
  --bg:#f1f3f7; --panel:#fff; --ink:#0f172a; --muted:#64748b; --faint:#94a3b8;
  --line:#e8ecf2; --accent:#3b6ef5; --track:#eef1f6; --shadow:0 1px 2px rgba(15,23,42,.05);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#0b0d12; --panel:#13161d; --ink:#e8eaf0; --muted:#8b93a7; --faint:#5d6579;
    --line:#1f242e; --track:#1b2029; --shadow:none;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#0b0d12; --panel:#13161d; --ink:#e8eaf0; --muted:#8b93a7; --faint:#5d6579;
  --line:#1f242e; --track:#1b2029; --shadow:none;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,"Segoe UI",Inter,system-ui,sans-serif}}
.shell{{display:grid;grid-template-columns:232px 1fr;min-height:100vh}}

aside{{background:var(--panel);border-right:1px solid var(--line);padding:22px 16px;
  display:flex;flex-direction:column}}
.brand{{display:flex;align-items:center;gap:10px;font-weight:650;font-size:15px;
  padding:0 8px 22px}}
.mark{{width:26px;height:26px;border-radius:8px;
  background:linear-gradient(135deg,#3b6ef5,#06b6d4)}}
.nav{{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:8px;
  color:var(--muted);text-decoration:none;font-size:13.5px;margin-bottom:2px}}
.nav:hover{{background:var(--track);color:var(--ink)}}
.nav.on{{background:var(--track);color:var(--accent);font-weight:600}}
.dot{{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.55}}
.side-foot{{margin-top:auto;background:linear-gradient(145deg,#1e293b,#0f172a);
  border-radius:12px;padding:16px;color:#cbd5e1}}
.side-foot b{{color:#fff;display:block;margin-bottom:4px;font-size:13px}}
.side-foot span{{font-size:11.5px;line-height:1.45;display:block;color:#94a3b8}}

main{{padding:24px 28px 40px;max-width:1500px}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}}
h1{{font-size:21px;margin:0;letter-spacing:-.01em}}
.sub{{color:var(--muted);font-size:12.5px;margin-top:3px}}
.chip{{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:7px 12px;font-size:12.5px;color:var(--muted)}}

.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
  gap:14px;margin-bottom:16px}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;box-shadow:var(--shadow)}}
.kpi-label{{font-size:12.5px;color:var(--muted)}}
.kpi-row{{display:flex;align-items:baseline;gap:9px;margin:7px 0 3px}}
.kpi-value{{font-size:28px;font-weight:650;letter-spacing:-.02em}}
.pill{{font-size:11px;font-weight:600;padding:2.5px 7px;border-radius:20px}}
.pill.up{{background:rgba(16,185,129,.12);color:#059669}}
.pill.down{{background:rgba(239,68,68,.12);color:#dc2626}}
.kpi-sub{{font-size:11.5px;color:var(--faint)}}

.grid{{display:grid;grid-template-columns:1.55fr 1fr;gap:14px;margin-bottom:14px}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px}}
@media (max-width:1050px){{.shell{{grid-template-columns:1fr}} aside{{display:none}}
  .grid,.grid3{{grid-template-columns:1fr}} main{{padding:20px 16px 32px}}}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;box-shadow:var(--shadow)}}
.card h2{{font-size:13.5px;margin:0 0 2px;font-weight:600}}
.card .cap{{font-size:11.5px;color:var(--faint);margin-bottom:16px}}
.empty{{color:var(--faint);font-size:12.5px;padding:14px 0}}

.frow{{display:grid;grid-template-columns:104px 1fr 44px;align-items:center;
  gap:12px;margin-bottom:9px}}
.fname{{font-size:12.5px;color:var(--muted)}}
.ftrack{{background:var(--track);border-radius:6px;height:10px;overflow:hidden}}
.fbar{{height:100%;border-radius:6px}}
.fval{{font-size:12.5px;font-weight:600;text-align:right}}

.donut{{width:160px;height:160px;display:block;margin:0 auto}}
.d-val{{text-anchor:middle;font-size:26px;font-weight:650;fill:var(--ink)}}
.d-cap{{text-anchor:middle;font-size:10px;fill:var(--faint);
  text-transform:uppercase;letter-spacing:.08em}}

.cols{{display:flex;align-items:flex-end;gap:16px;height:150px;padding-top:10px}}
.col{{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:flex-end;height:100%}}
.colnum{{font-size:12px;font-weight:600;margin-bottom:5px}}
.colbar{{width:100%;border-radius:7px 7px 3px 3px;min-height:5px}}
.collab{{font-size:11px;color:var(--faint);margin-top:8px}}

.mrow{{display:grid;grid-template-columns:1fr 70px 26px;align-items:center;
  gap:10px;margin-bottom:9px}}
.mname{{font-size:12.5px;color:var(--muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.mtrack{{background:var(--track);height:7px;border-radius:5px;overflow:hidden}}
.mbar{{height:100%;background:var(--accent);border-radius:5px}}
.mval{{font-size:12px;font-weight:600;text-align:right}}

table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:11px;color:var(--faint);font-weight:600;
  text-transform:uppercase;letter-spacing:.05em;padding:0 10px 10px}}
td{{padding:11px 10px;border-top:1px solid var(--line);font-size:12.5px;
  vertical-align:top}}
.co{{display:flex;align-items:center;gap:10px}}
.av{{width:30px;height:30px;border-radius:8px;background:var(--track);
  display:grid;place-items:center;font-weight:650;font-size:12px;color:var(--muted);
  flex:none}}
.co-name{{font-weight:600}}
.co-sub{{font-size:11px;color:var(--faint)}}
.score{{font-weight:650;padding:3px 9px;border-radius:7px;font-size:12px}}
.score.hot{{background:rgba(16,185,129,.13);color:#059669}}
.score.warm{{background:rgba(59,110,245,.12);color:#3b6ef5}}
.score.cool{{background:var(--track);color:var(--muted)}}
.sig{{color:var(--muted);max-width:230px}}
.angle{{color:var(--faint);max-width:330px;font-size:12px}}

.fail{{border-left:2px solid #ef4444;padding:2px 0 2px 11px;margin-bottom:13px}}
.fail.fixed{{border-left-color:#10b981}}
.fail-co{{font-weight:600;font-size:12.5px}}
.fail-note{{font-size:11.5px;color:var(--muted);margin-top:2px}}
.rev{{font-size:10px;font-weight:600;padding:1.5px 6px;border-radius:20px;
  margin-left:7px}}
.rev.ok{{background:rgba(16,185,129,.13);color:#059669}}
.rev.bad{{background:rgba(239,68,68,.12);color:#dc2626}}

.foot{{font-size:11px;color:var(--faint);margin-top:18px;line-height:1.6}}
</style></head><body>
<div class="shell">
  <aside>
    <div class="brand"><div class="mark"></div>Outbound Engine</div>
    {nav}
    <div class="side-foot">
      <b>Built for Rwazi</b>
      <span>Signal-based sourcing to QA-gated outreach, synced to HubSpot.
      Runs unattended on a schedule.</span>
    </div>
  </aside>
  <main>
    <header>
      <div>
        <h1>Pipeline overview</h1>
        <div class="sub">Expansion-signal sourcing across 5 emerging markets</div>
      </div>
      <div class="chip">Live from local pipeline database</div>
    </header>

    <div class="kpis">{kpis}</div>

    <div class="grid">
      <div class="card">
        <h2>Funnel</h2>
        <div class="cap">Where volume is removed, and at what cost</div>
        {_funnel_rows(m)}
      </div>
      <div class="card">
        <h2>QA gate</h2>
        <div class="cap">Blind second pass before anything is sendable</div>
        {_donut(m["qa_pass_rate"], m["revised"])}
      </div>
    </div>

    <div class="grid3">
      <div class="card">
        <h2>Score distribution</h2>
        <div class="cap">Weighted 4-factor rubric, 0 to 100</div>
        {_dist_bars(m["distribution"])}
      </div>
      <div class="card">
        <h2>Markets</h2>
        <div class="cap">Scored leads by target market</div>
        {_market_rows(m["markets"])}
      </div>
      <div class="card">
        <h2>Rejected by QA</h2>
        <div class="cap">{m["blocked_ever"]} drafts blocked before reaching a human</div>
        {_fail_rows(m["fails"])}
      </div>
    </div>

    <div class="card">
      <h2>Top scored leads</h2>
      <div class="cap">Ranked by rubric score, with the generated outreach angle</div>
      <table>
        <tr><th>Company</th><th>Score</th><th>Signal</th><th>Angle</th></tr>
        {_top_rows(m["top"])}
      </table>
    </div>

    <div class="foot">
      Keep rate counts only postings a model judged; the {m["prefiltered"]:,}
      rejected by deterministic rules never reached one.
      Hours saved applies a modeled 12 min/lead manual baseline to the
      {m["qa_pass"]} leads that reached a reviewable state, against measured
      review time. The manual baseline is modeled, not observed.
      Contact enrichment is a paid dependency; without it the engine produces
      company-level leads rather than inventing an email address.
    </div>
  </main>
</div>
</body></html>"""