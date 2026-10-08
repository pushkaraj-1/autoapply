"""Scanned jobs in data/jobs.db (SQLite). One row per job URL.

Replaces the email-outreach project's scan-history.tsv, dismissed.tsv and
deep-scores.tsv. Whether you applied is not kept here: it comes from the
tracker (data/tracker.json), so the board and the dashboard always agree.
"""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from jobautomate.profile import ROOT

PATH = ROOT / "data" / "jobs.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,          -- short hash of the URL, safe to put in links
    url TEXT UNIQUE NOT NULL,
    role_key TEXT NOT NULL,       -- company::title, to spot the same job on several boards
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    source TEXT,
    posted TEXT,                  -- real posting date when the source gives one
    first_seen TEXT,              -- date the scanner first found it
    fit INTEGER,                  -- title fit score
    matched TEXT,                 -- JSON: words behind the fit score
    deep_fit INTEGER,             -- AI fit score against the resume
    verdict TEXT,                 -- strong / possible / weak / no
    recommend INTEGER,
    reasons TEXT,                 -- JSON lists
    gaps TEXT,
    red_flags TEXT,
    jd_via TEXT,                  -- where the description came from, or no-jd
    description TEXT,
    scored_at TEXT,
    hidden INTEGER DEFAULT 0,
    opened_at TEXT,               -- when you clicked Apply on the board
    legacy_applied TEXT           -- applied date carried over from the old apply board
);
CREATE INDEX IF NOT EXISTS jobs_role_key ON jobs(role_key);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted);
CREATE TABLE IF NOT EXISTS queue (
    job_id TEXT PRIMARY KEY,      -- jobs.id
    added_at TEXT NOT NULL,
    status TEXT NOT NULL,         -- queued, running, submitted, needs_you, unconfirmed, ready, skipped, failed
    reasons TEXT,                 -- JSON list: why it needs you, or what went wrong
    ai_answers TEXT,              -- JSON list: answers the AI wrote, to read afterwards
    page_url TEXT,                -- the page after Submit (the confirmation page)
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS careers (
    company TEXT NOT NULL,        -- careers.company_key(name): "anduril" for "Anduril Industries"
    system TEXT NOT NULL,         -- workday, oracle, greenhouse, lever, ashby, successfactors, icims
    base TEXT NOT NULL,           -- where its jobs are searched: a board name or a site address
    source TEXT NOT NULL,         -- scan (seen in a scanned job), google, linkedin
    updated TEXT NOT NULL,
    PRIMARY KEY (company, system, base)
);
CREATE TABLE IF NOT EXISTS lookups (
    key TEXT PRIMARY KEY,         -- what was looked up, such as "google:anduril"
    result TEXT,                  -- what was found ("" for nothing)
    at TEXT NOT NULL
);
"""

LIST_COLUMNS = "id, url, title, company, location, source, posted, first_seen, fit, matched, deep_fit, verdict, recommend, reasons, gaps, red_flags, jd_via, hidden, opened_at, legacy_applied, ats_url, direct_url, ats_lookup"
# Columns added after the first release; each is added once to an existing database.
# ats_url: the job's own application form when it was found on the company's job board
# (for jobs listed on LinkedIn and other job sites); "" when looked for and not found.
# direct_url: the employer's own link a job site gave with the listing (Indeed and
# ZipRecruiter through JobSpy), often behind a click-tracking redirect.
# ats_lookup: which version of the form search last looked (see careers.LOOKUP_VERSION),
# so jobs not found before are looked at again once the search improves.
ADDED_COLUMNS = {"ats_url": "TEXT", "direct_url": "TEXT", "ats_lookup": "INTEGER"}
_migrated = False


def job_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


@contextmanager
def connect():
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(PATH)
        db.row_factory = sqlite3.Row
        try:
            global _migrated
            db.executescript(SCHEMA)
            if not _migrated:
                have = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
                for name, kind in ADDED_COLUMNS.items():
                    if name not in have:
                        db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
                _migrated = True
            yield db
            db.commit()
        finally:
            db.close()


def row_dict(row: sqlite3.Row) -> dict:
    job = dict(row)
    for key in ("matched", "reasons", "gaps", "red_flags"):
        if key in job:
            job[key] = json.loads(job[key]) if job[key] else ([] if key != "matched" else {})
    return job


def seen_sets() -> tuple[set[str], set[str]]:
    with connect() as db:
        rows = db.execute("SELECT url, role_key FROM jobs").fetchall()
    return {r["url"] for r in rows}, {r["role_key"] for r in rows}


def add_jobs(offers: list[dict]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as db:
        db.executemany(
            """INSERT OR IGNORE INTO jobs (id, url, role_key, title, company, location, source, posted, first_seen, fit, matched, description, direct_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (job_id(o["url"]), o["url"], o["role_key"], o["title"], o["company"], o["location"], o["source"], o.get("posted", ""), o.get("first_seen", today), o["fit"], json.dumps(o["matched"]), o.get("description") or None, o.get("direct_url") or None)
                for o in offers
            ],
        )


def fill_descriptions(offers: list[dict]) -> None:
    """Adds descriptions a source sent for jobs already saved without one."""
    with connect() as db:
        db.executemany(
            "UPDATE jobs SET description = ? WHERE url = ? AND (description IS NULL OR description = '')",
            [(o["description"], o["url"]) for o in offers if o.get("description")],
        )


def list_jobs(since: str | None = None) -> list[dict]:
    """Jobs posted (or first seen) on or after `since` (YYYY-MM-DD), newest first."""
    query = f"SELECT {LIST_COLUMNS} FROM jobs"
    params: tuple = ()
    if since:
        query += " WHERE COALESCE(NULLIF(posted, ''), first_seen) >= ?"
        params = (since,)
    with connect() as db:
        return [row_dict(r) for r in db.execute(query + " ORDER BY COALESCE(NULLIF(posted, ''), first_seen) DESC", params)]


def get_job(job_id_: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id_,)).fetchone()
    return row_dict(row) if row else None


def find_by_url(url: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return row_dict(row) if row else None


def update_job(job_id_: str, **fields) -> None:
    for key in ("reasons", "gaps", "red_flags", "matched"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])
    with connect() as db:
        db.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), job_id_))


def unscored(min_fit: int, limit: int, ids: list[str] | None = None) -> list[dict]:
    with connect() as db:
        if ids:
            marks = ",".join("?" * len(ids))
            rows = db.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", ids).fetchall()
        else:
            rows = db.execute("SELECT * FROM jobs WHERE deep_fit IS NULL AND hidden = 0 AND fit >= ? ORDER BY fit DESC, first_seen DESC LIMIT ?", (min_fit, limit)).fetchall()
    return [row_dict(r) for r in rows]


def counts() -> dict:
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        last = db.execute("SELECT MAX(first_seen) FROM jobs").fetchone()[0]
        today = db.execute("SELECT COUNT(*) FROM jobs WHERE first_seen = ? AND hidden = 0", (datetime.now().strftime("%Y-%m-%d"),)).fetchone()[0]
    return {"total": total, "last_seen": last, "today": today}


def queue_statuses() -> dict[str, str]:
    """Auto apply queue status by job id, for the Find jobs board."""
    with connect() as db:
        return {r["job_id"]: r["status"] for r in db.execute("SELECT job_id, status FROM queue")}
