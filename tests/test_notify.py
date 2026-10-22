"""Alerts must fire once per condition, survive a dead transport and stay off by default.

These tests drive the sweep directly against the test database. Transport is
monkeypatched, so nothing leaves the machine and the retry accounting is the only
thing under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import db, notify, worker
from common import config

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def make_run(conn, status: str = "tracking", reg_no: str | None = None) -> str:
    run_id = db.create_run(conn, language="en", raw_input="Records request")
    db.set_status(conn, run_id, status)
    if reg_no:
        db.set_run_fields(conn, run_id, reg_no=reg_no)
    return run_id


def flag_for_review(conn, due_at: str, reason: str = "Section 8(1)(j) personal data") -> str:
    run_id = make_run(conn, "review_queue")
    db.enqueue_review(conn, run_id, reason=reason, source="rule")
    conn.execute("UPDATE reviews SET due_at=? WHERE run_id=?", (due_at, run_id))
    return run_id


def rows(conn) -> list:
    return list(conn.execute("SELECT * FROM notifications ORDER BY created_at, rowid"))


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    """A developer's shell must not decide whether the transport tests pass."""
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NOTIFY_EMAIL_TO", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")


def test_an_overdue_review_alerts_once(conn):
    run_id = flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    created = notify.scan(conn, now=NOW)
    assert [row["kind"] for row in created] == ["review_overdue"]
    assert created[0]["run_id"] == run_id

    assert notify.scan(conn, now=NOW) == []
    recorded = rows(conn)
    assert len(recorded) == 1
    assert recorded[0]["status"] == "pending"


def test_a_review_outside_the_window_does_not_alert(conn):
    soon = (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    far = (NOW + timedelta(hours=48)).isoformat(timespec="seconds")
    flag_for_review(conn, soon)
    flag_for_review(conn, far, reason="a different flag")

    created = notify.scan(conn, now=NOW)
    assert [row["kind"] for row in created] == ["review_due_soon"]
    assert created[0]["payload"]["due_at"] == soon


def test_a_deadline_inside_the_horizon_alerts(conn):
    run_id = make_run(conn, "tracking", reg_no="MOCK/PWD/R/2026/00001")
    due = (NOW + timedelta(days=2)).isoformat(timespec="seconds")
    db.upsert_tracker(conn, run_id, reg_no="MOCK/PWD/R/2026/00001", due_date=due)

    created = notify.scan(conn, now=NOW)
    assert [row["kind"] for row in created] == ["deadline_soon"]
    assert "MOCK/PWD/R/2026/00001" in created[0]["payload"]["text"]


def test_a_draft_waiting_for_approval_and_a_failed_job_both_alert(conn):
    appeal = make_run(conn, "appeal_ready", reg_no="MOCK/PWD/R/2026/00002")
    failed = make_run(conn, "tracking")
    worker.enqueue_check(conn, failed)
    conn.execute(
        "UPDATE jobs SET status='failed', attempts=5, last_error='portal unreachable' WHERE run_id=?",
        (failed,),
    )

    created = notify.scan(conn, now=NOW)
    assert sorted(row["kind"] for row in created) == ["appeal_ready", "job_failed"]
    by_kind = {row["kind"]: row for row in created}
    assert by_kind["appeal_ready"]["run_id"] == appeal
    assert "portal unreachable" in by_kind["job_failed"]["payload"]["text"]


def test_disabled_does_nothing(conn):
    flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    assert notify.scan_and_send(conn, now=NOW) == {
        "created": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert rows(conn) == []


def test_a_webhook_receives_the_rendered_alert(conn, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "https://hooks.test/rti")
    delivered: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        notify, "_send_webhook", lambda url, payload: delivered.append((url, payload))
    )
    run_id = flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    stats = notify.scan_and_send(conn, now=NOW)

    assert stats == {"created": 1, "sent": 1, "failed": 0, "skipped": 0}
    url, payload = delivered[0]
    assert url == "https://hooks.test/rti"
    assert payload["kind"] == "review_overdue"
    assert run_id in payload["text"]
    assert payload["title"]
    stored = rows(conn)[0]
    assert stored["status"] == "sent"
    assert stored["attempts"] == 1
    assert stored["sent_at"]


def test_an_email_channel_is_a_second_target(conn, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    delivered: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        notify, "_send_email", lambda target, payload: delivered.append((target, payload))
    )
    flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    stats = notify.scan_and_send(conn, now=NOW)

    assert stats["sent"] == 1
    assert delivered[0][0] == "ops@example.test"
    assert rows(conn)[0]["channel"] == "email"


def test_a_failing_transport_retries_then_stops(conn, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK_URL", "https://hooks.test/rti")
    monkeypatch.setattr(config, "NOTIFY_MAX_ATTEMPTS", 2)

    def refuse(url: str, payload: dict) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(notify, "_send_webhook", refuse)
    flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    first = notify.scan_and_send(conn, now=NOW)
    assert first["failed"] == 1
    assert rows(conn)[0]["status"] == "pending"
    assert rows(conn)[0]["attempts"] == 1
    assert "connection refused" in rows(conn)[0]["last_error"]

    second = notify.scan_and_send(conn, now=NOW)
    assert second["failed"] == 1
    assert rows(conn)[0]["status"] == "failed"
    assert rows(conn)[0]["attempts"] == 2

    # A failed row is not retried forever, and the sweep still runs.
    assert notify.scan_and_send(conn, now=NOW)["failed"] == 0


def test_without_a_channel_the_alert_stays_pending(conn):
    flag_for_review(conn, "2026-01-01T00:00:00+00:00")

    created = notify.scan(conn, now=NOW)
    assert len(created) == 1
    assert notify.send_pending(conn) == {"sent": 0, "failed": 0, "skipped": 1}

    stored = rows(conn)[0]
    assert stored["status"] == "pending"
    assert stored["channel"] == ""


def test_recent_rows_come_back_with_a_decoded_payload(conn):
    flag_for_review(conn, "2026-01-01T00:00:00+00:00")
    notify.scan(conn, now=NOW)

    recent = notify.list_recent(conn)
    assert len(recent) == 1
    assert recent[0]["payload"]["kind"] == "review_overdue"
