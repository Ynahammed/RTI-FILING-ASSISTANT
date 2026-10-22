"""The SQLite-to-Postgres copy keeps the gate satisfied and is rerunnable.

Skipped unless ``RTI_TEST_POSTGRES_URL`` names a disposable database, because the
target schema is dropped between tests.
"""

from __future__ import annotations

import os

import pytest

from agent import db as agent_db
from common import database
from portal import db as portal_db
from scripts import migrate_to_postgres as migrate

URL = os.getenv("RTI_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not URL, reason="set RTI_TEST_POSTGRES_URL to run migration tests")


def _reset_target():
    conn = database.connect(URL)
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    return conn


def test_agent_rows_cross_the_trigger_gate(tmp_path):
    source = agent_db.connect(tmp_path / "agent.db")
    agent_db.init_db(source, force=True)
    run_id = agent_db.create_run(source, language="en", raw_input="Road repair spending")
    draft_id = agent_db.add_draft(
        source, run_id, kind="application", subject="Subject", text="Records",
        rewrite_notes=[],
    )
    agent_db.create_approval(source, run_id, draft_id, 10, "MH-PWD-01")
    agent_db.approve(source, run_id)
    agent_db.record_action(
        source, run_id, "submit", draft_id, "MH-PWD-01", "KEY-1", {},
        lambda: ({"ok": True}, "complete"),
    )
    agent_db.write_verification(
        source, run_id, status="verified", observed_reg_no="R/1", observed_deadline=None,
        expected_reg_no="R/1", expected_deadline=None, matches_intent=True, note="",
    )
    agent_db.upsert_tracker(source, run_id, reg_no="R/1", appeal_filed_at=agent_db.now_iso())
    agent_db.enqueue_review(source, run_id, reason="8(1)(j)", source="triage", score=0.8)
    source.close()

    target = _reset_target()
    try:
        first = migrate.migrate_agent(tmp_path / "agent.db", URL)
        assert first["rows_copied"] > 0
        assert target.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1
        assert target.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"] == 1
        assert target.execute("SELECT COUNT(*) AS n FROM verifications").fetchone()["n"] == 1
        assert target.execute("SELECT appeal_filed_at FROM tracker").fetchone()["appeal_filed_at"]
        # Explicit primary keys were copied; the serial sequence must continue
        # rather than hand out an id that already exists.
        agent_db.add_message(target, run_id, "user", "a message after migration")
        assert target.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] >= 1

        second = migrate.migrate_agent(tmp_path / "agent.db", URL)
        assert second["rows_copied"] == 0
        assert target.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1
    finally:
        target.close()


def test_portal_rows_and_sequences(tmp_path):
    source = portal_db.connect(tmp_path / "portal.db")
    portal_db.init_db(source)
    app, _ = portal_db.create_application(
        source,
        {
            "applicant_name": "Synthetic",
            "applicant_address": "Pune",
            "state": "Maharashtra",
            "department": "public_works",
            "authority_code": "MH-PWD-01",
            "information_sought": "Records",
            "idempotency_key": "IDEM-MIGRATE",
        },
    )
    portal_db.captcha_complete(source, app["app_id"])
    app = source.execute("SELECT * FROM applications WHERE app_id=?", (app["app_id"],)).fetchone()
    portal_db.record_payment(
        source, app, idempotency_key="PAY-MIGRATE", amount_inr=10,
        token_payload={"destination": "MH-PWD-01", "fee_inr": 10, "approval_id": "A-1"},
        token_present=True,
    )
    source.close()

    target = _reset_target()
    try:
        result = migrate.migrate_portal(tmp_path / "portal.db", URL)
        assert result["rows_copied"] > 0
        assert target.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"] == 1
        assert target.execute("SELECT COUNT(*) AS n FROM payment_attempts").fetchone()["n"] == 1
        # The serial sequence must continue after the copied event id, not restart.
        portal_db.log(target, None, "after-migration")
        ids = [row["event_id"] for row in target.execute("SELECT event_id FROM events")]
        assert len(ids) == len(set(ids))
    finally:
        target.close()


def test_dry_run_writes_nothing(tmp_path):
    source = agent_db.connect(tmp_path / "agent.db")
    agent_db.init_db(source, force=True)
    agent_db.create_run(source, language="en", raw_input="A request about road spending")
    source.close()

    target = _reset_target()
    try:
        result = migrate.migrate_agent(tmp_path / "agent.db", URL, dry_run=True)
        assert result["rows_copied"] == 0
        with pytest.raises(Exception):
            target.execute("SELECT COUNT(*) FROM runs")
    finally:
        target.close()
