"""Outbound alerts for work that would otherwise wait silently.

A flag in the review queue, a response deadline three days away, an appeal
drafted for approval and a deadline check that stopped retrying all need a
human. Before this module they needed one to open the dashboard first.

The sweep is a table, not an in-memory timer: every alert is a row in
`notifications` with a stable dedupe key, so a restart or a second worker tick
cannot send the same condition twice. Delivery is a webhook and/or email. With
neither configured the rows are still recorded and stay pending, which makes the
sweep safe to run on the demo that has no mail server.

Nothing here decides anything about a run. It reports what the database already
knows and never changes a run's status.
"""

from __future__ import annotations

import json
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from agent import db
from common import config

# Response periods that are being watched. A run whose reply is already late
# belongs to the tracker, not to this sweep; it still gets a deadline alert
# because the citizen is waiting either way.
DEADLINE_STATUSES = ("verified", "tracking", "appeal_filed", "second_appeal_ready")
APPEAL_STATUSES = ("appeal_ready", "second_appeal_ready", "complaint_ready")


def enabled() -> bool:
    """Alerts are opt-in: the demo has no mail server and should not need one."""
    return bool(config.NOTIFY_ENABLED)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _channels() -> list[tuple[str, str]]:
    """One entry per configured delivery channel, in a stable order."""
    channels: list[tuple[str, str]] = []
    if config.NOTIFY_WEBHOOK_URL:
        channels.append(("webhook", config.NOTIFY_WEBHOOK_URL))
    if config.NOTIFY_EMAIL_TO and config.SMTP_HOST:
        channels.append(("email", config.NOTIFY_EMAIL_TO))
    return channels


def _base_key(kind: str, run_id: str, boundary: str) -> str:
    return f"{kind}:{run_id}:{boundary}"


def _alert(
    *,
    kind: str,
    run_id: str,
    boundary: str,
    title: str,
    text: str,
    due_at: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "run_id": run_id,
        "boundary": boundary,
        "title": title,
        "text": text,
        "due_at": due_at,
        "url": f"{config.AGENT_URL}/",
    }


def _review_alerts(conn: sqlite3.Connection, moment: datetime) -> list[dict[str, Any]]:
    soon = moment + timedelta(hours=config.NOTIFY_DUE_SOON_HOURS)
    alerts: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT rv.run_id, rv.reason, rv.due_at, rv.assigned_to
             FROM reviews rv
            WHERE rv.status='open'"""
    ).fetchall()
    for row in rows:
        due = _parse(row["due_at"])
        if due is None:
            continue
        owner = row["assigned_to"] or "unassigned"
        if due <= moment:
            alerts.append(
                _alert(
                    kind="review_overdue",
                    run_id=row["run_id"],
                    boundary=row["due_at"],
                    title="A flagged request is past its review target",
                    text=(
                        f"Run {row['run_id']} has been waiting for review since "
                        f"{row['due_at']} ({owner}). Claim it and record a decision."
                    ),
                    due_at=row["due_at"],
                )
            )
        elif due <= soon:
            alerts.append(
                _alert(
                    kind="review_due_soon",
                    run_id=row["run_id"],
                    boundary=row["due_at"],
                    title="A flagged request is due for review",
                    text=(
                        f"Run {row['run_id']} is due for review at {row['due_at']} "
                        f"({owner}). Reason on the flag: {row['reason']}."
                    ),
                    due_at=row["due_at"],
                )
            )
    return alerts


def _deadline_alerts(conn: sqlite3.Connection, moment: datetime) -> list[dict[str, Any]]:
    horizon = moment + timedelta(hours=config.NOTIFY_DEADLINE_HOURS)
    placeholders = ",".join("?" for _ in DEADLINE_STATUSES)
    rows = conn.execute(
        f"""SELECT r.run_id, r.status, r.reg_no, t.due_date
              FROM runs r JOIN tracker t ON t.run_id = r.run_id
             WHERE r.status IN ({placeholders}) AND t.due_date IS NOT NULL""",
        DEADLINE_STATUSES,
    ).fetchall()
    alerts: list[dict[str, Any]] = []
    for row in rows:
        due = _parse(row["due_date"])
        if due is None or not (moment < due <= horizon):
            continue
        days = max(0, (due.date() - moment.date()).days)
        alerts.append(
            _alert(
                kind="deadline_soon",
                run_id=row["run_id"],
                boundary=row["due_date"],
                title="A response deadline is approaching",
                text=(
                    f"Request {row['reg_no'] or row['run_id']} is due on "
                    f"{row['due_date']} ({days} day(s)). If no reply arrives, the "
                    "tracker drafts a first appeal for approval."
                ),
                due_at=row["due_date"],
            )
        )
    return alerts


def _appeal_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in APPEAL_STATUSES)
    rows = conn.execute(
        f"SELECT run_id, status, reg_no FROM runs WHERE status IN ({placeholders})",
        APPEAL_STATUSES,
    ).fetchall()
    labels = {
        "appeal_ready": "A first appeal is drafted and waiting for approval",
        "second_appeal_ready": "A second appeal is drafted and waiting for approval",
        "complaint_ready": "A complaint is drafted and waiting for approval",
    }
    alerts: list[dict[str, Any]] = []
    for row in rows:
        alerts.append(
            _alert(
                kind="appeal_ready",
                run_id=row["run_id"],
                boundary=row["status"],
                title=labels.get(row["status"], "A draft is waiting for approval"),
                text=(
                    f"Run {row['run_id']} ({row['reg_no'] or 'no registration yet'}) has a "
                    f"draft in status {row['status']}. It cannot be filed without an "
                    "operator approving the exact card."
                ),
            )
        )
    return alerts


def _job_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT job_id, run_id, kind, attempts, last_error
             FROM jobs WHERE status='failed'"""
    ).fetchall()
    return [
        _alert(
            kind="job_failed",
            run_id=row["run_id"],
            boundary=row["job_id"],
            title="A deadline check gave up",
            text=(
                f"The {row['kind']} check for run {row['run_id']} failed after "
                f"{row['attempts']} attempt(s): {row['last_error'] or 'no error recorded'}. "
                "It will not retry; the run needs a person."
            ),
        )
        for row in rows
    ]


def scan(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """Record one pending notification row per condition per channel.

    Returns the rows this call created; a second call without a change creates
    none, because the dedupe key already exists.
    """
    moment = _utc(now)
    alerts = (
        _review_alerts(conn, moment)
        + _deadline_alerts(conn, moment)
        + _appeal_alerts(conn)
        + _job_alerts(conn)
    )
    if not alerts:
        return []

    channels = _channels() or [("", "")]
    created: list[dict[str, Any]] = []
    stamp = db.now_iso()
    for alert in alerts:
        base = _base_key(alert["kind"], alert["run_id"], alert["boundary"])
        payload = {
            "kind": alert["kind"],
            "run_id": alert["run_id"],
            "title": alert["title"],
            "text": alert["text"],
            "url": alert["url"],
            "due_at": alert["due_at"],
            "created_at": stamp,
        }
        for channel, target in channels:
            key = f"{base}:{channel}" if channel else base
            notification_id = db.new_id("NTF")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO notifications(
                       notification_id, kind, run_id, dedupe_key, channel, target,
                       payload, status, attempts, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    notification_id,
                    alert["kind"],
                    alert["run_id"],
                    key,
                    channel,
                    target,
                    json.dumps(payload),
                    stamp,
                ),
            )
            if cursor.rowcount:
                created.append(
                    {
                        "notification_id": notification_id,
                        "kind": alert["kind"],
                        "run_id": alert["run_id"],
                        "channel": channel,
                        "payload": payload,
                    }
                )
    return created


def _send_webhook(url: str, payload: dict[str, Any]) -> None:
    import httpx

    response = httpx.post(
        url, json=payload, timeout=config.NOTIFY_WEBHOOK_TIMEOUT_SECONDS
    )
    response.raise_for_status()


def _send_email(target: str, payload: dict[str, Any]) -> None:
    message = EmailMessage()
    message["Subject"] = f"[RTI Pilot] {payload.get('title', 'Notification')}"
    message["From"] = config.SMTP_FROM or config.SMTP_USER
    message["To"] = target
    message.set_content(payload.get("text", ""))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(message)


def _deliver(row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        payload = {"title": "RTI Pilot alert", "text": row["payload"] or ""}
    if row["channel"] == "webhook":
        _send_webhook(row["target"], payload)
    elif row["channel"] == "email":
        _send_email(row["target"], payload)
    else:
        raise RuntimeError("notification has no delivery channel")


def send_pending(conn: sqlite3.Connection, limit: int = 50) -> dict[str, int]:
    """Deliver pending rows. A failure is retried until the attempt cap."""
    channels = _channels()
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    pending = int(
        conn.execute("SELECT COUNT(*) AS n FROM notifications WHERE status='pending'")
        .fetchone()["n"]
    )
    if not channels:
        stats["skipped"] = pending
        return stats
    names = {name for name, _ in channels}
    rows = conn.execute(
        """SELECT * FROM notifications WHERE status='pending'
            ORDER BY created_at LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    for row in rows:
        if row["channel"] not in names:
            # Recorded while no channel was configured, or a channel was removed.
            stats["skipped"] += 1
            continue
        try:
            _deliver(row)
        except Exception as exc:  # a transport failure must not stop the sweep
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= config.NOTIFY_MAX_ATTEMPTS else "pending"
            conn.execute(
                """UPDATE notifications SET attempts=?, last_error=?, status=?
                    WHERE notification_id=?""",
                (attempts, f"{type(exc).__name__}: {exc}"[:300], status, row["notification_id"]),
            )
            stats["failed"] += 1
            continue
        conn.execute(
            """UPDATE notifications SET status='sent', attempts=attempts+1,
                   sent_at=?, last_error='' WHERE notification_id=?""",
            (db.now_iso(), row["notification_id"]),
        )
        stats["sent"] += 1
    return stats


def scan_and_send(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """One sweep for the worker loop. Never raises."""
    if not enabled():
        return {"created": 0, "sent": 0, "failed": 0, "skipped": 0}
    try:
        created = scan(conn, now=now)
        stats = send_pending(conn)
        return {"created": len(created), **stats}
    except Exception as exc:  # the deadline worker must survive a bad sweep
        return {
            "created": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def list_recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Recent rows for tests and inspection, with the payload decoded."""
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
        (max(1, min(500, limit)),),
    ):
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"] or "{}")
        except json.JSONDecodeError:
            pass
        out.append(item)
    return out
