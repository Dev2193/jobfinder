"""SQLite schema and connection helpers. See CLAUDE.md §2 for the schema spec."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "engine.db"

JOB_STATUSES = (
    "DISCOVERED",
    "SCORED",
    "TAILORED",
    "PREPPED",
    "AWAITING_APPROVAL",
    "SUBMITTED",
    "SKIPPED",
    "NEEDS_HUMAN",
    "DISCARDED",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    url             TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    work_mode       TEXT,
    employment_type TEXT,
    salary_min      INTEGER,
    salary_max      INTEGER,
    currency        TEXT,
    posted_at       TEXT,
    discovered_at   TEXT NOT NULL,
    description_raw TEXT,
    content_hash    TEXT NOT NULL,
    company_norm    TEXT NOT NULL,
    title_norm      TEXT NOT NULL,
    location_norm   TEXT,
    fit_score       INTEGER,
    status          TEXT NOT NULL DEFAULT 'DISCOVERED',
    skip_reason     TEXT,
    UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON jobs (content_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs (company_norm, title_norm, location_norm);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs (id),
    resume_path       TEXT,
    cover_letter_path TEXT,
    application_ref   TEXT,
    submitted_at      TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    tokens_spent      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications (job_id);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs (id),
    name            TEXT,
    role            TEXT,
    email           TEXT,
    email_confidence REAL,
    linkedin_url    TEXT,
    source_url      TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_job_id ON contacts (job_id);

CREATE TABLE IF NOT EXISTS outreach (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs (id),
    contact_id   INTEGER REFERENCES contacts (id),
    subject      TEXT,
    body         TEXT,
    status       TEXT NOT NULL DEFAULT 'DRAFT',
    approved_at  TEXT,
    sent_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outreach_job_id ON outreach (job_id);

CREATE TABLE IF NOT EXISTS token_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    stage          TEXT NOT NULL,
    job_id         INTEGER REFERENCES jobs (id),
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_token_ledger_stage ON token_ledger (stage);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    job_id   INTEGER REFERENCES jobs (id),
    type     TEXT NOT NULL,
    payload  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events (job_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);

CREATE TABLE IF NOT EXISTS resume_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    json_path    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_version_id  INTEGER NOT NULL REFERENCES resume_versions (id),
    created_at         TEXT NOT NULL,
    markdown_path      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'BLOCKING'
);

CREATE TABLE IF NOT EXISTS diagnosis_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id  INTEGER NOT NULL REFERENCES diagnoses (id),
    category      TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    waived        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_diagnosis_items_diagnosis_id ON diagnosis_items (diagnosis_id);

CREATE TABLE IF NOT EXISTS role_families (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key              TEXT NOT NULL UNIQUE,
    label                   TEXT NOT NULL,
    seniority               TEXT,
    missing_keywords        TEXT,
    recruiter_output        TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT,
    postings_at_last_run    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tailored_resumes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL UNIQUE REFERENCES jobs (id),
    content_hash        TEXT NOT NULL,
    rewritten_bullets    TEXT NOT NULL,
    before_after        TEXT,
    status              TEXT NOT NULL DEFAULT 'DRAFT',
    validator_attempts  INTEGER NOT NULL DEFAULT 0,
    validator_log       TEXT,
    docx_path           TEXT,
    ats_reparse_ok      INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_tailored_resumes_job_id ON tailored_resumes (job_id);

CREATE TABLE IF NOT EXISTS metrics_gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bullet_key      TEXT NOT NULL UNIQUE,
    org             TEXT NOT NULL,
    title           TEXT NOT NULL,
    original_bullet TEXT NOT NULL,
    needs_metric    TEXT NOT NULL,
    suggested_range TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    answer          TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS interview_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs (id),
    session_number    INTEGER NOT NULL,
    hireability_score INTEGER,
    transcript_path   TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interview_sessions_job_id ON interview_sessions (job_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_proposed_edits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL REFERENCES chat_messages (id),
    file_path    TEXT NOT NULL,
    old_content  TEXT,
    new_content  TEXT NOT NULL,
    diff         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_proposed_edits_message_id ON chat_proposed_edits (message_id);
"""

# Columns added after the initial schema design, applied idempotently so
# existing databases don't need to be dropped and recreated (§2: stages
# must be re-runnable without redoing expensive work, which implies the
# DB file itself should survive schema evolution).
_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, full ALTER TABLE ... ADD COLUMN ... clause)
    ("jobs", "role_family_id", "ALTER TABLE jobs ADD COLUMN role_family_id INTEGER REFERENCES role_families (id)"),
    ("tailored_resumes", "approved_at", "ALTER TABLE tailored_resumes ADD COLUMN approved_at TEXT"),
    ("tailored_resumes", "rejection_reason", "ALTER TABLE tailored_resumes ADD COLUMN rejection_reason TEXT"),
    ("applications", "screenshot_path", "ALTER TABLE applications ADD COLUMN screenshot_path TEXT"),
    ("applications", "blocked_reason", "ALTER TABLE applications ADD COLUMN blocked_reason TEXT"),
    ("applications", "unmapped_fields", "ALTER TABLE applications ADD COLUMN unmapped_fields TEXT"),
    ("applications", "filled_fields", "ALTER TABLE applications ADD COLUMN filled_fields TEXT"),
    ("applications", "created_at", "ALTER TABLE applications ADD COLUMN created_at TEXT"),
    ("applications", "updated_at", "ALTER TABLE applications ADD COLUMN updated_at TEXT"),
    ("tailored_resumes", "fits_one_page", "ALTER TABLE tailored_resumes ADD COLUMN fits_one_page INTEGER"),
    ("tailored_resumes", "trimmed_for_length", "ALTER TABLE tailored_resumes ADD COLUMN trimmed_for_length TEXT"),
    ("contacts", "kind", "ALTER TABLE contacts ADD COLUMN kind TEXT"),
    ("contacts", "discovered_at", "ALTER TABLE contacts ADD COLUMN discovered_at TEXT"),
    ("contacts", "needs_human", "ALTER TABLE contacts ADD COLUMN needs_human INTEGER NOT NULL DEFAULT 0"),
    ("contacts", "needs_human_reason", "ALTER TABLE contacts ADD COLUMN needs_human_reason TEXT"),
    ("outreach", "kind", "ALTER TABLE outreach ADD COLUMN kind TEXT NOT NULL DEFAULT 'hiring_manager_email'"),
    ("outreach", "validator_log", "ALTER TABLE outreach ADD COLUMN validator_log TEXT"),
    ("outreach", "rejection_reason", "ALTER TABLE outreach ADD COLUMN rejection_reason TEXT"),
    ("outreach", "follow_up_sent_at", "ALTER TABLE outreach ADD COLUMN follow_up_sent_at TEXT"),
    ("outreach", "created_at", "ALTER TABLE outreach ADD COLUMN created_at TEXT"),
    ("outreach", "updated_at", "ALTER TABLE outreach ADD COLUMN updated_at TEXT"),
    ("metrics_gaps", "gap_type", "ALTER TABLE metrics_gaps ADD COLUMN gap_type TEXT NOT NULL DEFAULT 'missing_metric'"),
    # Alumni-outreach enrichment (§8/§9): a fellow-alum contact at the target
    # company is a much warmer intro than a generic recruiter, so it's tracked
    # separately from the primary contact-discovery result rather than
    # overloading `kind`/`name`.
    ("contacts", "is_alumni", "ALTER TABLE contacts ADD COLUMN is_alumni INTEGER NOT NULL DEFAULT 0"),
    ("contacts", "alumni_school", "ALTER TABLE contacts ADD COLUMN alumni_school TEXT"),
    ("contacts", "alumni_search_url", "ALTER TABLE contacts ADD COLUMN alumni_search_url TEXT"),
    ("contacts", "alumni_name_hint", "ALTER TABLE contacts ADD COLUMN alumni_name_hint TEXT"),
    ("contacts", "alumni_source", "ALTER TABLE contacts ADD COLUMN alumni_source TEXT"),
    # The original uploaded resume file (PDF/DOCX), unmodified — per explicit
    # request, applications now submit this file exactly as uploaded rather
    # than a freshly rendered/tailored document, so the pipeline needs to
    # remember where it lives.
    ("resume_versions", "source_file_path", "ALTER TABLE resume_versions ADD COLUMN source_file_path TEXT"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for table, column, alter_sql in _MIGRATIONS:
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(alter_sql)


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_event(conn: sqlite3.Connection, event_type: str, payload: str = "", job_id: int | None = None) -> None:
    from datetime import datetime, timezone

    conn.execute(
        "INSERT INTO events (ts, job_id, type, payload) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), job_id, event_type, payload),
    )
