"""The same guarantees, exercised against Postgres when one is available.

These tests are skipped unless ``RTI_TEST_POSTGRES_URL`` names a disposable
database. CI provides one; locally:

    docker run -d --rm --name rti-pg -p 55432:5432 \
      -e POSTGRES_PASSWORD=rti -e POSTGRES_USER=rti -e POSTGRES_DB=rti \
      postgres:16-alpine
    RTI_TEST_POSTGRES_URL=postgresql://rti:rti@127.0.0.1:55432/rti python -m pytest tests/test_postgres.py

The database is treated as disposable: every fixture drops the public schema.
Never point this at a database you care about.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from agent import db, notify, worker
from common import config, database
from portal import db as portal_db

URL = os.getenv("RTI_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not URL, reason="set RTI_TEST_POSTGRES_URL to run Postgres tests")


def _reset(conn) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


@pytest.fixture()
def postgres():
    if not URL:
        pytest.skip("no Postgres test database")
    conn = database.connect(URL)
    _reset(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def agent(postgres, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DATABASE_URL", URL)
    db.init_db(postgres, force=True)
    return postgres


@pytest.fixture()
def portal(postgres, monkeypatch):
    monkeypatch.setattr(config, "PORTAL_DATABASE_URL", URL)
    _reset(postgres)
    portal_db.init_db(postgres)
    return postgres


def _run(conn) -> str:
    return db.create_run(conn, language="en", raw_input="How much was spent on the road?")


def _approved(conn, run_id: str) -> str:
    draft_id = db.add_draft(
        conn, run_id, kind="application", subject="Subject", text="Records requested.",
        rewrite_notes=[],
    )
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    db.approve(conn, run_id)
    return draft_id


# ------------------------------------------------------------------ guarantees


def test_schema_and_triggers_are_created(agent):
    tables = {
        row["name"]
        for row in agent.execute(
            "SELECT tablename AS name FROM pg_tables WHERE schemaname='public'"
        )
    }
    assert {"runs", "approvals", "actions", "verifications", "jobs", "notifications"} <= tables
    triggers = {
        row["name"]
        for row in agent.execute("SELECT tgname AS name FROM pg_trigger WHERE NOT tgisinternal")
    }
    assert triggers == {
        "actions_require_approval",
        "approvals_locked_after_approval",
        "verifications_verifier_only",
    }


def test_gate_blocks_an_unapproved_action(agent):
    run_id = _run(agent)
    draft_id = db.add_draft(
        agent, run_id, kind="application", subject="S", text="T", rewrite_notes=[]
    )
    with pytest.raises(Exception) as exc:
        agent.execute(
            """INSERT INTO actions(action_id, run_id, type, draft_id, destination,
                   idempotency_key, request, state, created_at, updated_at)
               VALUES ('ACT-X', ?, 'submit', ?, 'MH-PWD-01', 'KEY-X', '{}', 'pending', ?, ?)""",
            (run_id, draft_id, db.now_iso(), db.now_iso()),
        )
    assert "APPROVAL_REQUIRED" in str(exc.value)

    with pytest.raises(PermissionError):
        db.record_action(
            agent, run_id, "submit", draft_id, "MH-PWD-01", "KEY-X", {},
            lambda: ({}, "complete"),
        )


def test_an_approved_action_is_allowed(agent):
    run_id = _run(agent)
    draft_id = _approved(agent, run_id)
    row, executed = db.record_action(
        agent, run_id, "submit", draft_id, "MH-PWD-01", "KEY-OK", {"a": 1},
        lambda: ({"ok": True}, "complete"),
    )
    assert executed is True
    assert row["state"] == "complete"


def test_duplicate_idempotency_key_executes_once(agent):
    run_id = _run(agent)
    draft_id = _approved(agent, run_id)
    calls: list[int] = []

    def perform():
        calls.append(1)
        return {"ok": True}, "complete"

    db.record_action(agent, run_id, "submit", draft_id, "MH-PWD-01", "KEY-DUP", {}, perform)
    row, executed = db.record_action(
        agent, run_id, "submit", draft_id, "MH-PWD-01", "KEY-DUP", {}, perform
    )
    assert executed is False
    assert len(calls) == 1
    assert row["idempotency_key"] == "KEY-DUP"


def test_an_approved_approval_cannot_be_edited(agent):
    run_id = _run(agent)
    _approved(agent, run_id)
    with pytest.raises(Exception) as exc:
        agent.execute("UPDATE approvals SET token='replaced' WHERE run_id=?", (run_id,))
    assert "APPROVAL_LOCKED" in str(exc.value)


def test_verifications_require_a_live_lease(agent):
    run_id = _run(agent)
    with pytest.raises(Exception) as exc:
        agent.execute(
            """INSERT INTO verifications(verification_id, run_id, verification_status,
                   matches_intent, checked_at, writer_token)
               VALUES ('V-1', ?, 'verified', 1, ?, 'forged')""",
            (run_id, db.now_iso()),
        )
    assert "VERIFIER_LEASE_REQUIRED" in str(exc.value)

    db.write_verification(
        agent, run_id, status="verified", observed_reg_no="R/1", observed_deadline=None,
        expected_reg_no="R/1", expected_deadline=None, matches_intent=True, note="",
    )
    assert db.latest_verification(agent, run_id)["verification_status"] == "verified"


# ---------------------------------------------------------------- operations


def test_review_claim_and_decision(agent):
    run_id = _run(agent)
    db.enqueue_review(agent, run_id, reason="8(1)(j)", source="triage", score=0.8)
    operator = {"name": "operator", "operator_id": "OP-1"}
    review = db.claim_review(agent, run_id, operator)
    assert review["assigned_to"] == "operator"
    decided = db.decide_review(agent, run_id, operator, "release", "public interest")
    assert decided["decision"] == "release"


def test_notifications_are_deduplicated(agent):
    run_id = _run(agent)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    agent.execute(
        """INSERT INTO reviews(review_id, run_id, reason, source, status, due_at, created_at)
           VALUES ('REV-1', ?, 'flag', 'test', 'open', ?, ?)""",
        (run_id, past, db.now_iso()),
    )
    first = notify.scan(agent)
    second = notify.scan(agent)
    assert len(first) == 1
    assert second == []
    assert notify.list_recent(agent)[0]["kind"] == "review_overdue"


def test_job_claim_is_exclusive_and_reclaimable(agent):
    run_id = _run(agent)
    worker.enqueue_check(agent, run_id, due_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    first = worker.claim(agent, worker_id="w1", now=now)
    assert len(first) == 1
    assert worker.claim(agent, worker_id="w2", now=now) == []
    later = now + timedelta(minutes=5)
    assert len(worker.claim(agent, worker_id="w2", now=later)) == 1


def test_operator_lifecycle_and_audit(agent):
    operator_id, token = db.create_operator(agent, "alice")
    operator = db.operator_for_token(agent, token)
    assert operator["name"] == "alice"
    db.audit(agent, {"name": "alice", "operator_id": operator_id}, "test.action", None)
    assert db.audit_log(agent)[0]["action"] == "test.action"
    assert db.revoke_operator(agent, "alice") is True
    assert db.operator_for_token(agent, token) is None


def test_tracker_appeal_columns_round_trip(agent):
    run_id = _run(agent)
    db.upsert_tracker(agent, run_id, reg_no="R/1", appeal_filed_at=db.now_iso())
    db.upsert_tracker(agent, run_id, second_appeal_draft_id="D-2")
    tracker = db.get_tracker(agent, run_id)
    assert tracker["appeal_filed_at"]
    assert tracker["second_appeal_draft_id"] == "D-2"


def test_concurrent_job_claims_do_not_share_a_row(agent):
    run_id = _run(agent)
    for index in range(4):
        worker.enqueue_check(
            agent, run_id, due_at=datetime.now(timezone.utc), kind=f"thread-check-{index}"
        )

    barrier = threading.Barrier(2)
    claimed: dict[str, list[str]] = {"a": [], "b": []}

    def grab(name: str) -> None:
        conn = database.connect(URL)
        try:
            barrier.wait(timeout=5)
            rows = worker.claim(conn, worker_id=name, now=datetime.now(timezone.utc), limit=4)
            claimed[name] = [row["job_id"] for row in rows]
        finally:
            conn.close()

    threads = [threading.Thread(target=grab, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    all_ids = claimed["a"] + claimed["b"]
    assert len(all_ids) == 4
    assert len(all_ids) == len(set(all_ids))


# -------------------------------------------------------------------- portal


def test_portal_payment_files_once(portal):
    app, duplicate = portal_db.create_application(
        portal,
        {
            "applicant_name": "Synthetic Applicant",
            "applicant_address": "Ward 12, Pune",
            "state": "Maharashtra",
            "department": "public_works",
            "authority_code": "MH-PWD-01",
            "information_sought": "Road repair expenditure",
            "bpl": False,
            "urgent": False,
            "idempotency_key": "IDEM-1",
        },
    )
    assert duplicate is False
    _, duplicate_again = portal_db.create_application(
        portal,
        {
            "applicant_name": "Synthetic Applicant",
            "state": "Maharashtra",
            "department": "public_works",
            "authority_code": "MH-PWD-01",
            "information_sought": "Road repair expenditure",
            "idempotency_key": "IDEM-1",
        },
    )
    assert duplicate_again is True

    portal_db.captcha_complete(portal, app["app_id"])
    app = portal.execute("SELECT * FROM applications WHERE app_id=?", (app["app_id"],)).fetchone()
    rejected = portal_db.record_payment(
        portal, app, idempotency_key="PAY-0", amount_inr=10,
        token_payload=None, token_present=False,
    )
    assert rejected["ok"] is False and rejected["reason"] == "approval token missing"

    payload = {"destination": "MH-PWD-01", "fee_inr": 10, "approval_id": "A-1"}
    paid = portal_db.record_payment(
        portal, app, idempotency_key="PAY-1", amount_inr=10,
        token_payload=payload, token_present=True,
    )
    assert paid["ok"] is True
    assert paid["reg_no"].startswith("MOCK/PWD/R/")

    duplicate = portal_db.record_payment(
        portal, app, idempotency_key="PAY-1", amount_inr=10,
        token_payload=payload, token_present=True,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["reg_no"] == paid["reg_no"]


def test_portal_settings_use_insert_or_ignore(portal):
    before = portal_db.get_setting(portal, "fee_inr")
    portal_db.init_db(portal)
    assert portal_db.get_setting(portal, "fee_inr") == before
