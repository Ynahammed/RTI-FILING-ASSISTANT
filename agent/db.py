"""Agent state: runs, decisions, evidence, drafts, approvals, actions, verifications.

Three rules are enforced by the database itself, not by prompts:

1. An `actions` row of type payment or submit cannot be inserted unless an
   approved approval exists for the same draft and destination.
2. `actions.idempotency_key` is unique, so a double click or retry returns the
   stored result instead of filing again.
3. `verifications` can only be written with the verifier's live lease token.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import config

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
    last_checked_at TEXT
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

CREATE TABLE IF NOT EXISTS precedents (
    precedent_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL,
    outcome TEXT NOT NULL
);

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
           SELECT 1 FROM service_leases WHERE service = 'verifier' AND token = NEW.writer_token
       );
END;
"""

RUN_STATUSES = {
    "intake", "awaiting_input", "triaged", "review_queue", "redirected",
    "awaiting_approval", "declined", "awaiting_captcha", "filing", "filed",
    "filed_unverified", "verified", "tracking", "appeal_ready", "closed", "failed",
}


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.AGENT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    seed_precedents(conn)
    if own:
        return conn
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
        "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
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
    return conn.execute(
        "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
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
    except sqlite3.IntegrityError as exc:
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
    token = new_id("VLEASE")
    conn.execute(
        """INSERT INTO service_leases(service, token, updated_at) VALUES ('verifier', ?, ?)
           ON CONFLICT(service) DO UPDATE SET token=excluded.token, updated_at=excluded.updated_at""",
        (token, now_iso()),
    )
    return token


def release_verifier_lease(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM service_leases WHERE service='verifier'")


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
        release_verifier_lease(conn)
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
            "INSERT INTO tracker(run_id, reg_no, due_date, reply_state, appeal_draft_id, transferred_to, last_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, values.get("reg_no"), values.get("due_date"), values.get("reply_state", "awaiting"),
             values.get("appeal_draft_id"), values.get("transferred_to"), values["last_checked_at"]),
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
