from __future__ import annotations

import sqlite3

import pytest

from agent import db


def setup_run(conn):
    run_id = db.create_run(conn, language="en", raw_input="test request")
    draft_id = db.add_draft(
        conn, run_id, kind="application", subject="Test", text="Body", rewrite_notes=[],
    )
    return run_id, draft_id


def payment_action(**overrides):
    values = {
        "action_id": db.new_id("ACT"),
        "run_id": "RUN-X",
        "type": "payment",
        "draft_id": "DRF-X",
        "destination": "MH-PWD-01",
        "key": db.new_id("KEY"),
        "state": "pending",
        "ts": db.now_iso(),
    }
    values.update(overrides)
    return values


def insert_action(conn, values) -> None:
    conn.execute(
        """INSERT INTO actions(action_id, run_id, type, draft_id, destination, idempotency_key,
               request, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
        (
            values["action_id"], values["run_id"], values["type"], values["draft_id"],
            values["destination"], values["key"], values["state"], values["ts"], values["ts"],
        ),
    )


def test_payment_without_approval_is_rejected_by_the_database(conn):
    run_id, draft_id = setup_run(conn)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        insert_action(conn, payment_action(run_id=run_id, draft_id=draft_id))
    assert "APPROVAL_REQUIRED" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"] == 0


def test_declined_approval_does_not_unlock_actions(conn):
    run_id, draft_id = setup_run(conn)
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    db.decline(conn, run_id, "no")
    with pytest.raises(sqlite3.IntegrityError):
        insert_action(conn, payment_action(run_id=run_id, draft_id=draft_id))


def test_approved_approval_unlocks_payment(conn):
    run_id, draft_id = setup_run(conn)
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    db.approve(conn, run_id)
    insert_action(conn, payment_action(run_id=run_id, draft_id=draft_id))
    assert conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"] == 1


def test_approval_for_a_different_destination_does_not_unlock(conn):
    run_id, draft_id = setup_run(conn)
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    db.approve(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        insert_action(
            conn,
            payment_action(run_id=run_id, draft_id=draft_id, destination="MH-HOME-01"),
        )


def test_approved_approval_is_immutable(conn):
    run_id, draft_id = setup_run(conn)
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    approval = db.approve(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            "UPDATE approvals SET status='declined' WHERE approval_id=?", (approval["approval_id"],)
        )
    assert "APPROVAL_LOCKED" in str(exc.value)


def test_idempotency_key_files_once(conn):
    run_id, draft_id = setup_run(conn)
    db.create_approval(conn, run_id, draft_id, 10, "MH-PWD-01")
    db.approve(conn, run_id)
    calls = []

    def perform():
        calls.append(1)
        return {"ok": True}

    key = "fixed-key"
    _, executed = db.record_action(
        conn, run_id, "submit", draft_id, "MH-PWD-01", key, {"x": 1}, perform
    )
    _, executed_again = db.record_action(
        conn, run_id, "submit", draft_id, "MH-PWD-01", key, {"x": 1}, perform
    )
    assert executed is True
    assert executed_again is False
    assert calls == [1]
    assert conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"] == 1


def test_record_action_refuses_without_approval(conn):
    run_id, draft_id = setup_run(conn)
    with pytest.raises(PermissionError) as exc:
        db.record_action(
            conn, run_id, "payment", draft_id, "MH-PWD-01", "key-2", {}, lambda: {"ok": True}
        )
    assert "approval gate" in str(exc.value).lower()


def test_verifier_writes_require_the_verifier_lease(conn):
    run_id, _ = setup_run(conn)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            """INSERT INTO verifications(verification_id, run_id, verification_status, matches_intent,
                   checked_at, writer_token, note)
               VALUES ('VER-X', ?, 'verified', 1, ?, 'forged', '')""",
            (run_id, db.now_iso()),
        )
    assert "VERIFIER_LEASE_REQUIRED" in str(exc.value)

    verification_id = db.write_verification(
        conn, run_id, status="verified", observed_reg_no="MOCK/PWD/R/2026/00001",
        observed_deadline="2026-11-18T00:00:00", expected_reg_no="MOCK/PWD/R/2026/00001",
        expected_deadline="2026-11-18T00:00:00", matches_intent=True, note="ok",
    )
    assert verification_id.startswith("VER-")
    assert conn.execute("SELECT COUNT(*) AS n FROM service_leases").fetchone()["n"] == 0


def test_verifier_lease_can_be_reused_but_not_forged(conn):
    run_id, _ = setup_run(conn)
    lease = db.acquire_verifier_lease(conn)
    conn.execute(
        """INSERT INTO verifications(verification_id, run_id, verification_status, matches_intent,
               checked_at, writer_token, note)
           VALUES ('VER-Y', ?, 'pending', 0, ?, ?, 'held')""",
        (run_id, db.now_iso(), lease),
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM verifications").fetchone()["n"] == 1
