import sqlite3
import json
from contextlib import contextmanager
from engine.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    domain TEXT,
    ats_provider TEXT,
    ats_token TEXT,
    industry TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    source TEXT NOT NULL,              -- greenhouse | lever | news
    external_id TEXT NOT NULL,
    title TEXT,
    location TEXT,
    url TEXT,
    raw_excerpt TEXT,
    observed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    triage_status TEXT DEFAULT 'pending',   -- pending | signal | noise
    triage_market TEXT,
    triage_type TEXT,                  -- market_entry | distribution | insights_hire | other
    triage_reason TEXT,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    full_name TEXT,
    title TEXT,
    email TEXT,
    linkedin_url TEXT,
    source TEXT,
    confidence REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company_id, email)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id),
    total INTEGER NOT NULL,
    breakdown_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    model TEXT,
    scored_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    contact_id INTEGER REFERENCES contacts(id),
    step INTEGER NOT NULL DEFAULT 1,
    subject TEXT,
    body TEXT,
    claims_json TEXT,                  -- factual claims the model says it made
    model TEXT,
    status TEXT DEFAULT 'drafted',     -- drafted | qa_passed | qa_failed | approved | sent
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS qa_results (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    passed INTEGER NOT NULL,
    checks_json TEXT NOT NULL,
    notes TEXT,
    model TEXT,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def log_event(kind: str, payload: dict):
    with conn() as c:
        c.execute(
            "INSERT INTO events (kind, payload_json) VALUES (?, ?)",
            (kind, json.dumps(payload, default=str)),
        )


def upsert_company(name, domain=None, ats_provider=None, ats_token=None, industry=None) -> int:
    with conn() as c:
        c.execute(
            """INSERT INTO companies (name, domain, ats_provider, ats_token, industry)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 domain=COALESCE(excluded.domain, companies.domain),
                 ats_provider=COALESCE(excluded.ats_provider, companies.ats_provider),
                 ats_token=COALESCE(excluded.ats_token, companies.ats_token),
                 industry=COALESCE(excluded.industry, companies.industry)""",
            (name, domain, ats_provider, ats_token, industry),
        )
        return c.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()["id"]


def insert_signal(company_id, source, external_id, title, location, url, raw_excerpt) -> int | None:
    with conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO signals
               (company_id, source, external_id, title, location, url, raw_excerpt)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company_id, source, external_id, title, location, url, raw_excerpt),
        )
        return cur.lastrowid if cur.rowcount else None


def query(sql: str, params: tuple = ()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]