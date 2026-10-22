"""Agent state: runs, decisions, evidence, drafts, approvals, actions, verifications.

Three rules are enforced by the database itself, not by prompts:

1. An `actions` row of type payment or submit cannot be inserted unless an
   approved approval exists for the same draft and destination.
2. `actions.idempotency_key` is unique, so a double click or retry returns the
   stored result instead of filing again.
3. `verifications` can only be written with the verifier's live lease token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from common import config, database

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    overrides TEXT NOT NULL DEFAULT '{}',
    pending_question TEXT,
    missing_evidence TEXT,
    portal_app_id TEXT,
    reg_no TEXT,
    deadline TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    rule_ref TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT NOT NULL,
    extracted_value TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL,
    rewrite_notes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    fee_inr INTEGER NOT NULL,
    destination TEXT NOT NULL,
    token TEXT,
    approved_at TEXT,
    status TEXT NOT NULL,
    declined_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    draft_id TEXT,
    destination TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL DEFAULT '{}',
    response TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    observed_reg_no TEXT,
    observed_deadline TEXT,
    expected_reg_no TEXT,
    expected_deadline TEXT,
    matches_intent INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL,
    writer_token TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tracker (
    run_id TEXT PRIMARY KEY,
    reg_no TEXT,
    due_date TEXT,
    reply_state TEXT NOT NULL DEFAULT 'awaiting',
    appeal_draft_id TEXT,
    transferred_to TEXT,
    last_checked_at TEXT,
    appeal_filed_at TEXT,
    second_appeal_draft_id TEXT,
    complaint_draft_id TEXT
);

CREATE TABLE IF NOT EXISTS test_results (
    test_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    input TEXT NOT NULL,
    expected TEXT NOT NULL,
    actual TEXT NOT NULL,
    passed INTEGER NOT NULL,
    evidence_ref TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_leases (
    service TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per verifier run. A single global lease row was overwritten by every
-- concurrent verification, so two filings at once could invalidate each other's
-- token and abort a legitimate write.
CREATE TABLE IF NOT EXISTS verifier_tokens (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precedents (
    precedent_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL,
    outcome TEXT NOT NULL
);

-- A run is readable and answerable only by the client that created it, unless
-- the caller presents the operator token. The token is stored hashed.
CREATE TABLE IF NOT EXISTS run_tokens (
    run_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Durable deadline checks. A leased row is claimed by exactly one worker, and an
-- expired lease is reclaimable, so a crash cannot lose a filing's follow-up.
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    worker_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, due_at);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id, kind);

-- Operator accounts. The token is stored hashed, so a database read does not
-- yield a usable credential, and rotation is add-then-revoke rather than an edit
-- to one shared secret. The env token is bootstrapped as the first account.
CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'operator',
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS operator_audit (
    audit_id TEXT PRIMARY KEY,
    operator_id TEXT,
    operator_name TEXT NOT NULL,
    action TEXT NOT NULL,
    run_id TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- One row per flagged request. A flag is work with an owner and a deadline, not
-- a status a request can sit in unnoticed.
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    status TEXT NOT NULL DEFAULT 'open',
    assigned_to TEXT,
    assigned_at TEXT,
    due_at TEXT NOT NULL,
    decision TEXT,
    decided_by TEXT,
    decided_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reviews_open ON reviews(status, due_at);

-- Shared run-creation limit. In-process counters reset on restart and do not
-- coordinate two instances; this one lives beside the data it protects.
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    count INTEGER NOT NULL
);

-- Outbound alerts. A dedupe key makes the sweep idempotent: a restart or a second
-- worker tick cannot send the same overdue flag twice.
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS notifications_status ON notifications(status, created_at);

CREATE TRIGGER IF NOT EXISTS actions_require_approval
BEFORE INSERT ON actions
WHEN NEW.type IN ('payment', 'submit')
BEGIN
    SELECT RAISE(ABORT, 'APPROVAL_REQUIRED: no approved approval for this draft and destination')
    WHERE NOT EXISTS (
        SELECT 1 FROM approvals
        WHERE draft_id = NEW.draft_id
          AND destination = NEW.destination
          AND status = 'approved'
    );
END;

CREATE TRIGGER IF NOT EXISTS approvals_locked_after_approval
BEFORE UPDATE ON approvals
WHEN OLD.status = 'approved' AND (NEW.status <> 'approved' OR NEW.token IS NOT OLD.token)
BEGIN
    SELECT RAISE(ABORT, 'APPROVAL_LOCKED: an approved approval cannot be edited');
END;

CREATE TRIGGER IF NOT EXISTS verifications_verifier_only
BEFORE INSERT ON verifications
BEGIN
    SELECT RAISE(ABORT, 'VERIFIER_LEASE_REQUIRED: verifications are writable only by the verifier service')
    WHERE NEW.writer_token IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM verifier_tokens WHERE token = NEW.writer_token
       );
END;
"""

# Existing databases still carry the old trigger, which pointed at the single-row
# service_leases table. Replacing it is idempotent and keeps old data valid.
VERIFIER_TRIGGER = """
DROP TRIGGER IF EXISTS verifications_verifier_only;
CREATE TRIGGER verifications_verifier_only
BEFORE INSERT ON verifications
BEGIN
    SELECT RAISE(ABORT, 'VERIFIER_LEASE_REQUIRED: verifications are writable only by the verifier service')
    WHERE NEW.writer_token IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM verifier_tokens WHERE token = NEW.writer_token
       );
END;
"""

# The same three guarantees as the SQLite triggers in SCHEMA, written for
# Postgres. They live in the database, not the application, for the same reason:
# a bug in the agent must not be able to write a payment without an approval, edit
# an approved approval, or forge a verification.
POSTGRES_TRIGGERS = """
CREATE OR REPLACE FUNCTION rti_actions_require_approval() RETURNS trigger AS $$
BEGIN
    IF NEW.type IN ('payment', 'submit') AND NOT EXISTS (
        SELECT 1 FROM approvals
         WHERE draft_id = NEW.draft_id
           AND destination = NEW.destination
           AND status = 'approved'
    ) THEN
        RAISE EXCEPTION 'APPROVAL_REQUIRED: no approved approval for this draft and destination';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS actions_require_approval ON actions;
CREATE TRIGGER actions_require_approval
BEFORE INSERT ON actions
FOR EACH ROW EXECUTE FUNCTION rti_actions_require_approval();

CREATE OR REPLACE FUNCTION rti_approvals_locked_after_approval() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'approved'
       AND (NEW.status <> 'approved' OR NEW.token IS DISTINCT FROM OLD.token) THEN
        RAISE EXCEPTION 'APPROVAL_LOCKED: an approved approval cannot be edited';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS approvals_locked_after_approval ON approvals;
CREATE TRIGGER approvals_locked_after_approval
BEFORE UPDATE ON approvals
FOR EACH ROW EXECUTE FUNCTION rti_approvals_locked_after_approval();

CREATE OR REPLACE FUNCTION rti_verifications_verifier_only() RETURNS trigger AS $$
BEGIN
    IF NEW.writer_token IS NULL OR NOT EXISTS (
        SELECT 1 FROM verifier_tokens WHERE token = NEW.writer_token
    ) THEN
        RAISE EXCEPTION 'VERIFIER_LEASE_REQUIRED: verifications are writable only by the verifier service';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS verifications_verifier_only ON verifications;
CREATE TRIGGER verifications_verifier_only
BEFORE INSERT ON verifications
FOR EACH ROW EXECUTE FUNCTION rti_verifications_verifier_only();
"""

RUN_STATUSES = {
    "intake", "awaiting_input", "triaged", "review_queue", "redirected",
    "awaiting_approval", "declined", "awaiting_captcha", "filing", "filed",
    "filed_unverified", "verified", "tracking", "appeal_ready", "appeal_filed",
    "second_appeal_ready", "complaint_ready", "closed", "failed",
}


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the configured backend, or the SQLite file for local development."""
    if config.AGENT_DATABASE_URL:
        return database.connect(config.AGENT_DATABASE_URL)
    return database.connect(db_path or config.AGENT_DB)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def bootstrap_operator(conn: sqlite3.Connection, token: str | None = None) -> None:
    """Make the configured operator token a real account once.

    Only when there are no accounts at all: a later rotation must not be undone by
    a restart with the old env value still set.
    """
    if conn.execute("SELECT 1 FROM operators LIMIT 1").fetchone():
        return
    token = token or config.OPERATOR_TOKEN
    if not token:
        return
    conn.execute(
        "INSERT INTO operators(operator_id, name, token_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
        (new_id("OP"), "operator", _hash_token(token), now_iso()),
    )


# Columns added after a database already exists. `CREATE TABLE IF NOT EXISTS`
# does not alter an existing table, so an upgrade needs an explicit ALTER. Each
# entry is (table, column, declaration); the helper checks PRAGMA table_info first.
MIGRATIONS = [
    ("tracker", "appeal_filed_at", "TEXT"),
    ("tracker", "second_appeal_draft_id", "TEXT"),
    ("tracker", "complaint_draft_id", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, declaration in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db(conn: sqlite3.Connection | None = None, *, force: bool = False) -> sqlite3.Connection:
    """Create the schema once per database file per process.

    This used to run `executescript(SCHEMA)` on every run creation. Under
    concurrency that takes a write lock for DDL on every request and serialises
    the whole service, which showed up as p95 latency rather than as an error.
    """
    conn = conn or connect()
    if database.is_postgres(conn):
        location = database.describe(conn)
    else:
        row = conn.execute("PRAGMA database_list").fetchone()
        location = str(row["file"] or "")
    if force or location not in _schema_ready:
        with _schema_lock:
            if force or location not in _schema_ready:
                conn.executescript(SCHEMA)
                if database.is_postgres(conn):
                    # The shared SCHEMA carries SQLite trigger bodies for their
                    # WHEN clauses; Postgres gets explicit PL/pgSQL equivalents.
                    conn.executescript(POSTGRES_TRIGGERS)
                else:
                    conn.executescript(VERIFIER_TRIGGER)
                _migrate(conn)
                seed_precedents(conn)
                bootstrap_operator(conn)
                _schema_ready.add(location)
    return conn


def seed_precedents(conn: sqlite3.Connection) -> None:
    rows = [
        ("PRE-001", "Road repair expenditure, Pune ward 12", "public_works",
         "road,repair,expenditure,ward,pune,voucher,work order",
         "Drafted as numbered record requests: work orders, measurement books, vouchers."),
        ("PRE-002", "Street light maintenance contract, Hyderabad", "municipal",
         "street light,maintenance,contract,hyderabad,ghmc",
         "Asked for the contract, payment records and inspection register."),
        ("PRE-003", "Pothole repair timeline, Bengaluru", "public_works",
         "pothole,road,repair,bengaluru,bbmp,timeline",
         "Split into records held plus dates of completion; avoided asking why."),
    ]
    for pid, title, dept, keywords, outcome in rows:
        conn.execute(
            """INSERT INTO precedents(precedent_id, title, department, keywords, outcome)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(precedent_id) DO UPDATE SET
               title=excluded.title, department=excluded.department,
               keywords=excluded.keywords, outcome=excluded.outcome""",
            (pid, title, dept, keywords, outcome),
        )


# ------------------------------------------------------------------ run tokens


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_run_token(conn: sqlite3.Connection, run_id: str) -> str:
    """Mint the one credential that scopes a client to this run.

    Returned once, at creation, and never stored in plaintext: losing it means
    starting a new run, not reading someone else's.
    """
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO run_tokens(run_id, token_hash, created_at) VALUES (?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET token_hash=excluded.token_hash,
               created_at=excluded.created_at""",
        (run_id, _hash_token(token), now_iso()),
    )
    return token


def verify_run_token(conn: sqlite3.Connection, run_id: str, token: str) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT token_hash FROM run_tokens WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return False
    return hmac.compare_digest(row["token_hash"], _hash_token(token))


# ------------------------------------------------------------------ operators


def create_operator(conn: sqlite3.Connection, name: str, role: str = "operator") -> tuple[str, str]:
    """Create an account and return its token once. The database keeps only the hash."""
    name = name.strip()
    if not name:
        raise ValueError("an operator needs a name")
    if conn.execute("SELECT 1 FROM operators WHERE name=?", (name,)).fetchone():
        raise ValueError(f"an operator named {name!r} already exists")
    token = secrets.token_urlsafe(32)
    operator_id = new_id("OP")
    conn.execute(
        "INSERT INTO operators(operator_id, name, token_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (operator_id, name, _hash_token(token), role, now_iso()),
    )
    return operator_id, token


def operator_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM operators WHERE token_hash=? AND revoked_at IS NULL",
        (_hash_token(token),),
    ).fetchone()
    return row


def list_operators(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT operator_id, name, role, created_at, revoked_at FROM operators ORDER BY created_at"
    )
    return [dict(row) for row in rows]


def revoke_operator(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "UPDATE operators SET revoked_at=? WHERE name=? AND revoked_at IS NULL",
        (now_iso(), name),
    )
    return cur.rowcount > 0


def audit(
    conn: sqlite3.Connection,
    operator: dict[str, Any] | None,
    action: str,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record who did what. Every operator mutation writes one of these."""
    conn.execute(
        "INSERT INTO operator_audit(audit_id, operator_id, operator_name, action, run_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("AUD"),
            (operator or {}).get("operator_id"),
            (operator or {}).get("name") or "unknown",
            action,
            run_id,
            json.dumps(detail or {}, ensure_ascii=False),
            now_iso(),
        ),
    )


def audit_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM operator_audit ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(1, min(500, limit)),),
    )
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["detail"] = json.loads(item["detail"] or "{}")
        except json.JSONDecodeError:
            pass
        out.append(item)
    return out


# --------------------------------------------------------------- review queue

DEFAULT_REVIEW_SLA_HOURS = 48


def enqueue_review(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    reason: str,
    source: str,
    score: float | None = None,
    sla_hours: int | None = None,
) -> None:
    """Flag a run for human review with an owner slot and an explicit due time."""
    from datetime import timedelta

    hours = DEFAULT_REVIEW_SLA_HOURS if sla_hours is None else sla_hours
    due = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO reviews(review_id, run_id, reason, source, score, due_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO NOTHING""",
        (new_id("REV"), run_id, reason, source, score, due, now_iso()),
    )


def get_review(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reviews WHERE run_id=?", (run_id,)).fetchone()


def list_reviews(conn: sqlite3.Connection, status: str | None = "open", limit: int = 100) -> list[dict[str, Any]]:
    sql = """SELECT rv.*, r.raw_input, r.language, r.status AS run_status, r.created_at AS run_created_at
             FROM reviews rv JOIN runs r ON r.run_id = rv.run_id"""
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE rv.status=?"
        params = (status,)
    sql += " ORDER BY (rv.status='open') DESC, rv.due_at ASC LIMIT ?"
    params += (max(1, min(500, limit)),)
    now = now_iso()
    out = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["overdue"] = item["status"] == "open" and item["due_at"] < now
        out.append(item)
    return out


def claim_review(conn: sqlite3.Connection, run_id: str, operator: dict[str, Any]) -> sqlite3.Row:
    """Take responsibility for an open review. Idempotent for the same operator.

    A second operator cannot silently take over a review that is already assigned;
    the queue is small and a lost assignment is a lost decision.
    """
    review = get_review(conn, run_id)
    if not review:
        raise KeyError(run_id)
    if review["status"] != "open":
        raise ValueError("this review has already been decided")
    name = operator.get("name") or "operator"
    if review["assigned_to"] and review["assigned_to"] != name:
        raise ValueError(f"this review is assigned to {review['assigned_to']}")
    if not review["assigned_to"]:
        conn.execute(
            "UPDATE reviews SET assigned_to=?, assigned_at=? WHERE run_id=? AND assigned_to IS NULL",
            (name, now_iso(), run_id),
        )
    audit(conn, operator, "review.claim", run_id)
    refreshed = get_review(conn, run_id)
    assert refreshed is not None
    return refreshed


def decide_review(
    conn: sqlite3.Connection,
    run_id: str,
    operator: dict[str, Any],
    decision: str,
    note: str = "",
) -> sqlite3.Row:
    """Close a review. The deciding operator must hold the assignment."""
    if decision not in {"release", "uphold"}:
        raise ValueError("decision must be 'release' or 'uphold'")
    review = get_review(conn, run_id)
    if not review:
        raise KeyError(run_id)
    if review["status"] != "open":
        raise ValueError("this review has already been decided")
    name = operator.get("name") or "operator"
    if review["assigned_to"] != name:
        raise ValueError("claim this review before deciding it")
    if decision == "release" and not note.strip():
        raise ValueError("releasing a flagged request needs a short reason on the record")
    conn.execute(
        """UPDATE reviews SET status='decided', decision=?, decided_by=?, decided_at=?, note=?
           WHERE run_id=?""",
        (decision, name, now_iso(), note.strip(), run_id),
    )
    audit(conn, operator, f"review.{decision}", run_id, {"note": note.strip()})
    decided = get_review(conn, run_id)
    assert decided is not None
    return decided


def review_counts(conn: sqlite3.Connection) -> dict[str, int]:
    now = now_iso()
    open_rows = conn.execute("SELECT due_at FROM reviews WHERE status='open'").fetchall()
    return {
        "open": len(open_rows),
        "overdue": sum(1 for row in open_rows if row["due_at"] < now),
        "unassigned": sum(
            1 for row in conn.execute("SELECT assigned_to FROM reviews WHERE status='open'")
            if not row["assigned_to"]
        ),
    }


# ----------------------------------------------------------------- rate limit


def check_rate_limit(conn: sqlite3.Connection, key: str, limit: int, window_seconds: float = 60.0) -> None:
    """Sliding-window limit shared through SQLite, so a restart or a second
    instance does not hand out a fresh budget."""
    if limit <= 0:
        return
    import time as _time

    from fastapi import HTTPException

    now = _time.time()
    row = conn.execute("SELECT window_start, count FROM rate_limits WHERE key=?", (key,)).fetchone()
    if row and now - float(row["window_start"]) <= window_seconds:
        if int(row["count"]) >= limit:
            retry_after = max(1, int(window_seconds - (now - float(row["window_start"]))))
            raise HTTPException(
                status_code=429,
                detail="too many runs created; try again shortly",
                headers={"Retry-After": str(retry_after)},
            )
        conn.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))
    else:
        conn.execute(
            """INSERT INTO rate_limits(key, window_start, count) VALUES (?, ?, 1)
               ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start, count=1""",
            (key, now),
        )


# ------------------------------------------------------------------- run basics


def create_run(conn: sqlite3.Connection, *, language: str, raw_input: str, overrides: dict[str, Any] | None = None) -> str:
    run_id = new_id("RUN")
    ts = now_iso()
    conn.execute(
        "INSERT INTO runs(run_id, language, raw_input, status, created_at, updated_at, overrides) VALUES (?, ?, ?, 'intake', ?, ?, ?)",
        (run_id, language, raw_input, ts, ts, json.dumps(overrides or {})),
    )
    add_evidence(
        conn, run_id,
        source_id="INPUT-1", source_url="", extracted_value="Citizen request captured as data, not instructions",
        source="user", kind="input", retrieved_at=ts,
    )
    return run_id


def set_status(conn: sqlite3.Connection, run_id: str, status: str, *, missing: str | None = None) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown status {status}")
    conn.execute(
        "UPDATE runs SET status=?, updated_at=?, missing_evidence=? WHERE run_id=?",
        (status, now_iso(), missing, run_id),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise KeyError(run_id)
    return row


def set_run_fields(conn: sqlite3.Connection, run_id: str, **values: Any) -> None:
    allowed = {"pending_question", "portal_app_id", "reg_no", "deadline", "status", "language", "overrides"}
    updates = {k: v for k, v in values.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE runs SET {sets}, updated_at=? WHERE run_id=?",
        (*updates.values(), now_iso(), run_id),
    )


def add_message(conn: sqlite3.Connection, run_id: str, role: str, text: str) -> None:
    conn.execute(
        "INSERT INTO messages(run_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        (run_id, role, text, now_iso()),
    )


def add_field(conn: sqlite3.Connection, run_id: str, name: str, value: str, confidence: float, source: str) -> None:
    conn.execute(
        """INSERT INTO fields(run_id, name, value, confidence, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(run_id, name) DO UPDATE SET value=excluded.value,
           confidence=excluded.confidence, source=excluded.source""",
        (run_id, name, str(value), confidence, source),
    )


def add_decision(
    conn: sqlite3.Connection, run_id: str, agent: str, label: str, confidence: float,
    reason: str, rule_ref: str, output: dict[str, Any] | None = None,
) -> str:
    decision_id = new_id("DEC")
    conn.execute(
        "INSERT INTO decisions(decision_id, run_id, agent, label, confidence, reason, rule_ref, output, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, run_id, agent, label, confidence, reason, rule_ref, json.dumps(output or {}), now_iso()),
    )
    return decision_id


def add_evidence(
    conn: sqlite3.Connection, run_id: str, *, source_id: str, source_url: str,
    extracted_value: str, source: str, kind: str, retrieved_at: str | None = None,
) -> str:
    evidence_id = new_id("EVD")
    conn.execute(
        """INSERT INTO evidence(evidence_id, run_id, source_id, source_url, retrieved_at,
               extracted_value, source, kind, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (evidence_id, run_id, source_id, source_url, retrieved_at or now_iso(),
         extracted_value, source, kind, now_iso()),
    )
    return evidence_id


def add_draft(
    conn: sqlite3.Connection, run_id: str, *, kind: str, subject: str, text: str,
    rewrite_notes: list[str], version: int | None = None,
) -> str:
    draft_id = new_id("DRF")
    if version is None:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM drafts WHERE run_id=?", (run_id,)).fetchone()
        version = int(row["v"]) + 1
    conn.execute(
        """INSERT INTO drafts(draft_id, run_id, version, kind, subject, text, rewrite_notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft_id, run_id, version, kind, subject, text, json.dumps(rewrite_notes), now_iso()),
    )
    return draft_id


def latest_draft(conn: sqlite3.Connection, run_id: str, kind: str = "application") -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drafts WHERE run_id=? AND kind=? ORDER BY version DESC LIMIT 1", (run_id, kind)
    ).fetchone()


# --------------------------------------------------------------------- approval


def create_approval(conn: sqlite3.Connection, run_id: str, draft_id: str, fee_inr: int, destination: str) -> str:
    approval_id = new_id("APR")
    conn.execute(
        """INSERT INTO approvals(approval_id, run_id, draft_id, fee_inr, destination, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (approval_id, run_id, draft_id, fee_inr, destination, now_iso()),
    )
    return approval_id


def approve(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
    ).fetchone()
    if not row:
        raise ValueError("no approval card exists for this run")
    if row["status"] == "declined":
        raise ValueError("this approval was declined; start a new draft")
    if row["status"] == "approved":
        return row

    from common.crypto import sign_approval

    approved_at = now_iso()
    payload = {
        "approval_id": row["approval_id"],
        "run_id": run_id,
        "draft_id": row["draft_id"],
        "fee_inr": row["fee_inr"],
        "destination": row["destination"],
        "approved_at": approved_at,
    }
    token = sign_approval(payload)
    conn.execute(
        "UPDATE approvals SET status='approved', token=?, approved_at=? WHERE approval_id=?",
        (token, approved_at, row["approval_id"]),
    )
    add_evidence(
        conn, run_id, source_id=row["approval_id"], source_url="",
        extracted_value=f"Human approved filing to {row['destination']} at fee Rs {row['fee_inr']}",
        source="approval", kind="approval", retrieved_at=approved_at,
    )
    return conn.execute("SELECT * FROM approvals WHERE approval_id=?", (row["approval_id"],)).fetchone()


def decline(conn: sqlite3.Connection, run_id: str, reason: str) -> None:
    row = conn.execute(
        "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError("no approval card exists for this run")
    if row["status"] == "approved":
        raise ValueError("an approved filing cannot be declined")
    conn.execute(
        "UPDATE approvals SET status='declined', declined_reason=? WHERE approval_id=?",
        (reason, row["approval_id"]),
    )
    add_evidence(
        conn, run_id, source_id=row["approval_id"], source_url="",
        extracted_value=f"Human declined filing: {reason or 'no reason given'}",
        source="approval", kind="approval",
    )


def current_approval(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    # Two cards created in the same second used to be ordered arbitrarily; the
    # appeal ladder creates a second approval for the same run.
    return conn.execute(
        "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()


# ---------------------------------------------------------------------- actions


def record_action(
    conn: sqlite3.Connection, run_id: str, action_type: str, draft_id: str, destination: str,
    key: str, request: dict[str, Any], perform: Callable[[], dict[str, Any]],
) -> tuple[sqlite3.Row, bool]:
    """Insert, execute and complete an action exactly once per idempotency key.

    Returns (row, executed_now). The INSERT is what the approval trigger guards,
    so no portal call can happen before an approval exists.
    """
    existing = conn.execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        return existing, False

    action_id = new_id("ACT")
    ts = now_iso()
    try:
        conn.execute(
            """INSERT INTO actions(action_id, run_id, type, draft_id, destination, idempotency_key,
                   request, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (action_id, run_id, action_type, draft_id, destination, key, json.dumps(request), ts, ts),
        )
    except database.INTEGRITY_ERRORS as exc:
        if "APPROVAL_REQUIRED" in str(exc):
            raise PermissionError(
                "Blocked by the approval gate: no approved approval exists for this draft and destination."
            ) from exc
        existing = conn.execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return existing, False
        raise

    try:
        result = perform()
        if isinstance(result, tuple):
            response, state = result
        else:
            response, state = result, "complete"
    except Exception as exc:  # transport failure: keep the action for inspection
        response = {"error": str(exc)}
        state = "unknown"
    conn.execute(
        "UPDATE actions SET response=?, state=?, updated_at=? WHERE action_id=?",
        (json.dumps(response, default=str), state, now_iso(), action_id),
    )
    return conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone(), True


def actions_for(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM actions WHERE run_id=? ORDER BY created_at", (run_id,)))


# ------------------------------------------------------------------ verifier


def acquire_verifier_lease(conn: sqlite3.Connection) -> str:
    """Mint a verifier token. Tokens are independent, so verifications can overlap."""
    token = new_id("VLEASE")
    conn.execute(
        "INSERT INTO verifier_tokens(token, created_at) VALUES (?, ?)", (token, now_iso())
    )
    # Housekeeping: a token is only needed for the duration of one write. The
    # cutoff is computed here because SQLite's datetime() has no Postgres twin.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM verifier_tokens WHERE created_at < ?", (cutoff,))
    return token


def release_verifier_lease(conn: sqlite3.Connection, token: str | None = None) -> None:
    if token is None:
        conn.execute("DELETE FROM verifier_tokens")
        return
    conn.execute("DELETE FROM verifier_tokens WHERE token=?", (token,))


def write_verification(conn: sqlite3.Connection, run_id: str, *, status: str, observed_reg_no: str | None,
                       observed_deadline: str | None, expected_reg_no: str | None,
                       expected_deadline: str | None, matches_intent: bool, note: str) -> str:
    token = acquire_verifier_lease(conn)
    verification_id = new_id("VER")
    try:
        conn.execute(
            """INSERT INTO verifications(verification_id, run_id, verification_status, observed_reg_no,
                   observed_deadline, expected_reg_no, expected_deadline, matches_intent, checked_at,
                   writer_token, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (verification_id, run_id, status, observed_reg_no, observed_deadline,
             expected_reg_no, expected_deadline, int(matches_intent), now_iso(), token, note),
        )
    finally:
        release_verifier_lease(conn, token)
    return verification_id


def latest_verification(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verifications WHERE run_id=? ORDER BY checked_at DESC LIMIT 1", (run_id,)
    ).fetchone()


# -------------------------------------------------------------------- tracker


def upsert_tracker(conn: sqlite3.Connection, run_id: str, **values: Any) -> None:
    row = conn.execute("SELECT run_id FROM tracker WHERE run_id=?", (run_id,)).fetchone()
    values["last_checked_at"] = now_iso()
    if not row:
        conn.execute(
            """INSERT INTO tracker(
                   run_id, reg_no, due_date, reply_state, appeal_draft_id, transferred_to,
                   last_checked_at, appeal_filed_at, second_appeal_draft_id, complaint_draft_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, values.get("reg_no"), values.get("due_date"), values.get("reply_state", "awaiting"),
             values.get("appeal_draft_id"), values.get("transferred_to"), values["last_checked_at"],
             values.get("appeal_filed_at"), values.get("second_appeal_draft_id"),
             values.get("complaint_draft_id")),
        )
        return
    sets = ", ".join(f"{k}=?" for k in values)
    conn.execute(f"UPDATE tracker SET {sets} WHERE run_id=?", (*values.values(), run_id))


def get_tracker(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tracker WHERE run_id=?", (run_id,)).fetchone()


# ---------------------------------------------------------------------- detail


def run_detail(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = dict(get_run(conn, run_id))
    run["overrides"] = json.loads(run["overrides"] or "{}")
    detail: dict[str, Any] = {
        "run": run,
        "messages": [dict(r) for r in conn.execute(
            "SELECT role, text, created_at FROM messages WHERE run_id=? ORDER BY message_id", (run_id,)
        )],
        "fields": {
            r["name"]: {"value": r["value"], "confidence": r["confidence"], "source": r["source"]}
            for r in conn.execute("SELECT * FROM fields WHERE run_id=?", (run_id,))
        },
        "decisions": [
            {**dict(r), "output": json.loads(r["output"] or "{}")}
            for r in conn.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY created_at, rowid", (run_id,))
        ],
        "evidence": [dict(r) for r in conn.execute(
            "SELECT * FROM evidence WHERE run_id=? ORDER BY created_at, rowid", (run_id,)
        )],
        "drafts": [
            {**dict(r), "rewrite_notes": json.loads(r["rewrite_notes"] or "[]")}
            for r in conn.execute("SELECT * FROM drafts WHERE run_id=? ORDER BY version", (run_id,))
        ],
        "approval": None,
        "actions": [dict(r) for r in actions_for(conn, run_id)],
        "verification": None,
        "tracker": None,
    }
    approval = current_approval(conn, run_id)
    if approval:
        detail["approval"] = dict(approval)
    verification = latest_verification(conn, run_id)
    if verification:
        detail["verification"] = dict(verification)
    tracker = get_tracker(conn, run_id)
    if tracker:
        detail["tracker"] = dict(tracker)
    for action in detail["actions"]:
        try:
            action["request"] = json.loads(action["request"])
            action["response"] = json.loads(action["response"])
        except (TypeError, json.JSONDecodeError):
            pass
    return detail


def save_test_result(
    conn: sqlite3.Connection, *, test_id: str, run_id: str, name: str, input_text: str,
    expected: str, actual: str, passed: bool, evidence_ref: str = "", notes: str = "",
) -> None:
    conn.execute(
        """INSERT INTO test_results(test_id, run_id, name, input, expected, actual, passed, evidence_ref, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(test_id) DO UPDATE SET run_id=excluded.run_id, name=excluded.name,
               input=excluded.input, expected=excluded.expected, actual=excluded.actual,
               passed=excluded.passed, evidence_ref=excluded.evidence_ref,
               notes=excluded.notes, created_at=excluded.created_at""",
        (test_id, run_id, name, input_text, expected, actual, int(passed), evidence_ref, notes, now_iso()),
    )
