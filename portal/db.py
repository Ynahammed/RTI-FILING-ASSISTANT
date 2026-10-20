"""Persistence for the mock portal.

The portal keeps its own database. It never reads the agent's database; the
only shared state is the HMAC approval-token secret.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import config

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorities (
    code TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    state TEXT NOT NULL,
    department TEXT NOT NULL,
    department_label TEXT NOT NULL,
    name TEXT NOT NULL,
    pio TEXT NOT NULL,
    address TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    app_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    applicant_name TEXT NOT NULL,
    applicant_address TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    department TEXT NOT NULL,
    authority_code TEXT NOT NULL,
    information_sought TEXT NOT NULL,
    bpl INTEGER NOT NULL DEFAULT 0,
    urgent INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    captcha_passed INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    reg_no TEXT UNIQUE,
    filed_at TEXT,
    deadline TEXT,
    displayed_deadline TEXT,
    confirmation_available INTEGER NOT NULL DEFAULT 1,
    transfer_to_code TEXT,
    transfer_at TEXT
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    amount_inr INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    reason TEXT,
    approval_id TEXT,
    reg_no TEXT,
    created_at TEXT NOT NULL
);

-- Only an accepted payment is deduplicated: one filing per key. Rejected
-- attempts are kept as evidence and may be retried with a corrected token.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_paid_key
    ON payment_attempts(app_id, idempotency_key) WHERE accepted = 1;

CREATE TABLE IF NOT EXISTS replies (
    reply_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    body TEXT NOT NULL,
    evasive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.PORTAL_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    seed_authorities(conn)
    defaults = {
        "mock_now": iso(utc_now().astimezone(IST)),
        "timeout_next_filing": "0",
        "skew_next_deadline_days": "0",
        "fee_inr": "10",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    if own:
        return conn
    return conn


def seed_authorities(conn: sqlite3.Connection) -> None:
    registry = json.loads((config.PROJECT_ROOT / "rules" / "authority_registry.json").read_text(encoding="utf-8"))
    for auth in registry["authorities"]:
        conn.execute(
            """
            INSERT INTO authorities(code, level, state, department, department_label, name, pio,
                                    address, source_id, source_url, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                level=excluded.level, state=excluded.state, department=excluded.department,
                department_label=excluded.department_label, name=excluded.name, pio=excluded.pio,
                address=excluded.address, source_id=excluded.source_id,
                source_url=excluded.source_url, retrieved_at=excluded.retrieved_at
            """,
            (
                auth["code"], auth["level"], auth["state"], auth["department"],
                auth["department_label"], auth["name"], auth["pio"], auth["address"],
                auth["source_id"], auth["source_url"], auth["retrieved_at"],
            ),
        )


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def mock_now(conn: sqlite3.Connection) -> datetime:
    raw = get_setting(conn, "mock_now")
    try:
        return datetime.fromisoformat(raw).astimezone(IST)
    except ValueError:
        return utc_now().astimezone(IST)


def advance_clock(conn: sqlite3.Connection, days: int) -> datetime:
    now = mock_now(conn) + timedelta(days=days)
    set_setting(conn, "mock_now", iso(now))
    log(conn, None, "clock_advanced", f"{days} days -> {iso(now)}")
    return now


def log(conn: sqlite3.Connection, app_id: str | None, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events(app_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
        (app_id, event, detail, iso(utc_now())),
    )


def fee_for(bpl: bool) -> int:
    return 0 if bpl else 10


def canonical_authority(conn: sqlite3.Connection, state: str, department: str) -> sqlite3.Row | None:
    level = "central" if state == "Central" else "state"
    return conn.execute(
        "SELECT * FROM authorities WHERE level=? AND state=? AND department=? ORDER BY code LIMIT 1",
        (level, state, department),
    ).fetchone()


def authority(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM authorities WHERE code=?", (code,)).fetchone()


def all_authorities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM authorities ORDER BY level DESC, state, department"))


def dept_code(authority_code: str) -> str:
    parts = authority_code.split("-")
    return parts[1] if len(parts) > 1 else "GEN"


def next_registration(conn: sqlite3.Connection, authority_code: str, filed_at: datetime) -> str:
    dept = dept_code(authority_code)
    year = filed_at.year
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE reg_no LIKE ?",
        (f"MOCK/{dept}/R/{year}/%",),
    ).fetchone()
    return f"MOCK/{dept}/R/{year}/{(row['n'] + 1):05d}"


def compute_deadline(filed_at: datetime, urgent: bool) -> datetime:
    return filed_at + (timedelta(hours=48) if urgent else timedelta(days=30))


def create_application(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[sqlite3.Row, bool]:
    """Insert an application. Returns (row, duplicate)."""
    existing = conn.execute(
        "SELECT * FROM applications WHERE idempotency_key=?", (payload["idempotency_key"],)
    ).fetchone()
    if existing:
        return existing, True
    app_id = f"APP-{uuid.uuid4().hex[:10].upper()}"
    conn.execute(
        """
        INSERT INTO applications(app_id, created_at, applicant_name, applicant_address, state,
            department, authority_code, information_sought, bpl, urgent, idempotency_key, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'captcha_pending')
        """,
        (
            app_id, iso(utc_now()), payload["applicant_name"], payload.get("applicant_address", ""),
            payload["state"], payload["department"], payload["authority_code"],
            payload["information_sought"], int(payload.get("bpl", False)),
            int(payload.get("urgent", False)), payload["idempotency_key"],
        ),
    )
    log(conn, app_id, "application_received", payload["authority_code"])
    return conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone(), False


def captcha_complete(conn: sqlite3.Connection, app_id: str) -> None:
    conn.execute(
        "UPDATE applications SET captcha_passed=1, status='captcha_passed' WHERE app_id=? AND captcha_passed=0",
        (app_id,),
    )
    conn.execute("UPDATE applications SET status='captcha_passed' WHERE app_id=? AND status='captcha_pending'", (app_id,))
    log(conn, app_id, "captcha_completed", "demo operator handoff")


def record_payment(
    conn: sqlite3.Connection,
    app: sqlite3.Row,
    *,
    idempotency_key: str,
    amount_inr: int,
    token_payload: dict[str, Any] | None,
    token_present: bool,
) -> dict[str, Any]:
    """Validate and record a payment. Returns a result dict with `ok` and `reason`."""
    # A duplicate *accepted* payment with the same idempotency key returns the
    # stored receipt instead of filing again.
    prior = conn.execute(
        "SELECT * FROM payment_attempts WHERE app_id=? AND idempotency_key=? AND accepted=1",
        (app["app_id"], idempotency_key),
    ).fetchone()
    if prior:
        log(conn, app["app_id"], "payment_duplicate", idempotency_key)
        return {"ok": True, "duplicate": True, "reason": "duplicate idempotency key", "reg_no": prior["reg_no"]}

    expected_fee = fee_for(bool(app["bpl"]))
    reason = ""
    if not token_present:
        reason = "approval token missing"
    elif token_payload is None:
        reason = "approval token invalid"
    elif token_payload.get("destination") != app["authority_code"]:
        reason = "approval token destination mismatch"
    elif int(token_payload.get("fee_inr", -1)) != expected_fee:
        reason = "approval token fee mismatch"
    elif amount_inr != expected_fee:
        reason = f"amount must be Rs {expected_fee}"
    elif not app["captcha_passed"]:
        reason = "captcha not completed"

    attempt_id = f"PAY-{uuid.uuid4().hex[:10].upper()}"
    now = mock_now(conn)
    if reason:
        conn.execute(
            """INSERT INTO payment_attempts(attempt_id, app_id, idempotency_key, amount_inr,
                   accepted, reason, approval_id, reg_no, created_at)
               VALUES (?, ?, ?, ?, 0, ?, ?, NULL, ?)""",
            (attempt_id, app["app_id"], idempotency_key, amount_inr, reason,
             (token_payload or {}).get("approval_id"), iso(now)),
        )
        log(conn, app["app_id"], "payment_rejected", reason)
        return {"ok": False, "duplicate": False, "reason": reason, "reg_no": None}

    # Accepted payment: file the application.
    timeout = get_setting(conn, "timeout_next_filing", "0") == "1"
    skew_days = int(get_setting(conn, "skew_next_deadline_days", "0") or 0)
    if timeout:
        set_setting(conn, "timeout_next_filing", "0")
    if skew_days:
        set_setting(conn, "skew_next_deadline_days", "0")

    reg_no = next_registration(conn, app["authority_code"], now)
    deadline = compute_deadline(now, bool(app["urgent"]))
    displayed = deadline + timedelta(days=skew_days)
    canonical = canonical_authority(conn, app["state"], app["department"])
    transfer_to = None
    if canonical and canonical["code"] != app["authority_code"]:
        transfer_to = canonical["code"]

    conn.execute(
        """UPDATE applications SET status=?, captcha_passed=1, paid_at=?, reg_no=?, filed_at=?,
               deadline=?, displayed_deadline=?, confirmation_available=?, transfer_to_code=?, transfer_at=?
           WHERE app_id=?""",
        (
            "transferred" if transfer_to else "filed",
            iso(now), reg_no, iso(now), iso(deadline), iso(displayed),
            0 if timeout else 1, transfer_to, iso(now) if transfer_to else None, app["app_id"],
        ),
    )
    conn.execute(
        """INSERT INTO payment_attempts(attempt_id, app_id, idempotency_key, amount_inr,
               accepted, reason, approval_id, reg_no, created_at)
           VALUES (?, ?, ?, ?, 1, NULL, ?, ?, ?)""",
        (attempt_id, app["app_id"], idempotency_key, amount_inr,
         (token_payload or {}).get("approval_id"), reg_no, iso(now)),
    )
    log(conn, app["app_id"], "payment_accepted", f"{reg_no} timeout={timeout}")
    if transfer_to:
        log(conn, app["app_id"], "transferred", f"section 6(3) -> {transfer_to}")
    return {
        "ok": True,
        "duplicate": False,
        "reason": "",
        "reg_no": reg_no,
        "timeout": timeout,
        "deadline": iso(deadline),
        "displayed_deadline": iso(displayed),
        "app_id": app["app_id"],
    }


def add_reply(conn: sqlite3.Connection, app_id: str, body: str, evasive: bool) -> None:
    conn.execute(
        "INSERT INTO replies(reply_id, app_id, body, evasive, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"REP-{uuid.uuid4().hex[:8].upper()}", app_id, body, int(evasive), iso(mock_now(conn))),
    )
    log(conn, app_id, "reply_recorded", "evasive" if evasive else "substantive")
