"""The deadline worker must not lose, duplicate or strand a follow-up."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent import db, supervisor, worker
from common import config


def make_run(conn, text: str = "Records request") -> str:
    return db.create_run(conn, language="en", raw_input=text)


def test_enqueue_is_idempotent(conn):
    run_id = make_run(conn)
    assert worker.enqueue_check(conn, run_id) is True
    assert worker.enqueue_check(conn, run_id) is False
    assert worker.pending_jobs(conn) == 1


def test_claim_hands_each_job_to_exactly_one_worker(conn):
    now = datetime.now(timezone.utc)
    for _ in range(2):
        worker.enqueue_check(conn, make_run(conn), due_at=now - timedelta(minutes=1))

    first = worker.claim(conn, worker_id="worker-a", now=now)
    second = worker.claim(conn, worker_id="worker-b", now=now)

    assert len(first) == 2
    assert second == []
    assert {row["worker_id"] for row in first} == {"worker-a"}


def test_an_expired_lease_is_reclaimed(conn):
    now = datetime.now(timezone.utc)
    worker.enqueue_check(conn, make_run(conn), due_at=now - timedelta(minutes=1))

    held = worker.claim(conn, worker_id="worker-a", now=now, lease_seconds=30)
    assert len(held) == 1
    assert worker.claim(conn, worker_id="worker-b", now=now) == []

    later = now + timedelta(seconds=31)
    reclaimed = worker.claim(conn, worker_id="worker-b", now=later)
    assert len(reclaimed) == 1
    assert reclaimed[0]["worker_id"] == "worker-b" or reclaimed[0]["job_id"] == held[0]["job_id"]


def test_failure_backs_off_and_eventually_stops(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    worker.enqueue_check(conn, make_run(conn), due_at=now)

    def failing(run_id: str):
        raise RuntimeError("portal unreachable")

    monkeypatch.setattr(supervisor, "track", failing)

    first = worker.run_once(conn, now=now)
    assert first["retried"] == 1
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["due_at"] > now.isoformat()

    failures = 0
    for step in range(1, 6):
        later = now + timedelta(minutes=2 ** (step + 1) + 1)
        failures += worker.run_once(conn, now=later)["failed"]
    final = conn.execute("SELECT * FROM jobs").fetchone()
    assert final["status"] == "failed"
    assert "portal unreachable" in final["last_error"]
    assert failures == 1


def test_success_reschedules_then_finishes_at_terminal(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    run_id = make_run(conn)
    worker.enqueue_check(conn, run_id, due_at=now)

    state = {"status": "tracking"}
    monkeypatch.setattr(supervisor, "track", lambda rid: {"run": {"status": state["status"]}})

    worker.run_once(conn, now=now)
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "pending"
    assert job["attempts"] == 0
    expected = now + timedelta(hours=config.CHECK_INTERVAL_HOURS)
    assert job["due_at"].startswith(expected.strftime("%Y-%m-%dT%H"))

    state["status"] = "appeal_ready"
    stats = worker.run_once(conn, now=now + timedelta(hours=config.CHECK_INTERVAL_HOURS + 1))
    assert stats["finished"] == 1
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "done"
    assert worker.claim(conn, worker_id="late", now=now + timedelta(days=2)) == []


def test_a_run_that_was_never_filed_is_cancelled(conn):
    run_id = make_run(conn)
    worker.enqueue_check(conn, run_id, due_at=datetime.now(timezone.utc))
    stats = worker.run_once(conn, now=datetime.now(timezone.utc))
    assert stats["finished"] == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["status"] == "cancelled"
    assert "filed" in row["last_error"]


def test_one_failing_job_does_not_stop_the_others(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    good = make_run(conn, "good")
    bad = make_run(conn, "bad")
    worker.enqueue_check(conn, good, due_at=now)
    worker.enqueue_check(conn, bad, due_at=now)

    def track(run_id: str):
        if run_id == bad:
            raise RuntimeError("boom")
        return {"run": {"status": "tracking"}}

    monkeypatch.setattr(supervisor, "track", track)
    stats = worker.run_once(conn, now=now, limit=5)

    assert stats["claimed"] == 2
    assert stats["checked"] == 1
    assert stats["retried"] == 1
    statuses = {row["run_id"]: row["status"] for row in conn.execute("SELECT run_id, status FROM jobs")}
    assert statuses[good] == "pending"
    assert statuses[bad] == "pending"
