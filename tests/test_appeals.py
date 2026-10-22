"""The appeal ladder: first appeal on record, then Section 19(3) or Section 18.

A first appeal used to be the end of the line. These tests pin the next rung:
the Commission's forty-five day window under Section 19(6), the second appeal
that follows silence, the complaint that follows an evasive reply, and the fact
that a human approval is still required at every step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent import db, supervisor, tracker, worker

DESTINATION = "MH-PWD-01"


def seed_first_appeal(conn, *, due: str = "2026-11-19T00:00:00+05:30") -> str:
    """A filed run whose first appeal has been drafted and is ready to approve."""
    run_id = db.create_run(conn, language="en", raw_input="Road repair records, Ward 12, Pune")
    db.set_run_fields(
        conn, run_id, portal_app_id="APP-1", reg_no="REG-1", deadline=due,
    )
    db.set_status(conn, run_id, "tracking")
    draft_id = db.add_draft(
        conn, run_id, kind="appeal_first",
        subject="First appeal under Section 19 — REG-1",
        text="First appeal under Section 19 of the Right to Information Act, 2005.",
        rewrite_notes=["Response period expired."],
    )
    db.create_approval(conn, run_id, draft_id, 0, DESTINATION)
    # Backdate the first-appeal card: approval timestamps have one-second
    # resolution, and the ladder must select the newer card unambiguously.
    conn.execute(
        "UPDATE approvals SET created_at='2026-10-01T00:00:00+00:00' WHERE run_id=?", (run_id,)
    )
    db.upsert_tracker(
        conn, run_id, reg_no="REG-1", due_date=due, reply_state="no_reply",
        appeal_draft_id=draft_id,
    )
    db.set_status(conn, run_id, "appeal_ready")
    return run_id


def clock_after(conn, run_id: str, days: int) -> str:
    filed = datetime.fromisoformat(db.get_tracker(conn, run_id)["appeal_filed_at"])
    return (filed + timedelta(days=days)).astimezone(timezone.utc).isoformat()


def fake_portal(monkeypatch, *, clock: str, replies: list[dict] | None = None) -> None:
    payloads = {
        "/api/status/APP-1": {"reg_no": "REG-1"},
        "/api/clock": {"mock_now": clock},
        "/api/replies/APP-1": replies or [],
    }
    monkeypatch.setattr(tracker, "_get_json", lambda path: payloads.get(path))


def draft_rows(conn, run_id: str, kind: str):
    return conn.execute(
        "SELECT * FROM drafts WHERE run_id=? AND kind=? ORDER BY version", (run_id, kind)
    ).fetchall()


def test_approving_the_first_appeal_arms_the_tracker(conn):
    run_id = seed_first_appeal(conn)

    detail = supervisor.approve_appeal(run_id)

    assert detail["run"]["status"] == "appeal_filed"
    row = db.get_tracker(conn, run_id)
    assert row["appeal_filed_at"]
    assert worker.pending_jobs(conn) == 1
    approval = db.current_approval(conn, run_id)
    assert approval["status"] == "approved"
    assert any(e["kind"] == "appeal" and "APPEAL-FILED" == e["source_id"] for e in detail["evidence"])


def test_nothing_is_escalated_before_the_decision_window(conn, monkeypatch):
    run_id = seed_first_appeal(conn)
    supervisor.approve_appeal(run_id)
    fake_portal(monkeypatch, clock=clock_after(conn, run_id, 10))

    result = tracker.track(conn, run_id)

    assert result["appeal"] is None
    assert db.get_run(conn, run_id)["status"] == "appeal_filed"
    assert draft_rows(conn, run_id, "appeal_second") == []


def test_silence_after_the_window_drafts_one_second_appeal(conn, monkeypatch):
    run_id = seed_first_appeal(conn)
    supervisor.approve_appeal(run_id)
    fake_portal(monkeypatch, clock=clock_after(conn, run_id, 46))

    result = tracker.track(conn, run_id)

    assert result["appeal"] is not None
    drafts = draft_rows(conn, run_id, "appeal_second")
    assert len(drafts) == 1
    text = drafts[0]["text"]
    assert "Section 19(3)" in text
    assert "Section 19(6)" in text
    assert "Section 20" in text

    run = db.get_run(conn, run_id)
    assert run["status"] == "second_appeal_ready"
    assert db.get_tracker(conn, run_id)["second_appeal_draft_id"] == drafts[0]["draft_id"]
    labels = {
        row["label"] for row in conn.execute(
            "SELECT label FROM decisions WHERE run_id=? AND agent='tracker'", (run_id,)
        )
    }
    assert "second_appeal_drafted" in labels
    assert db.current_approval(conn, run_id)["status"] == "pending"


def test_the_second_appeal_is_drafted_only_once(conn, monkeypatch):
    run_id = seed_first_appeal(conn)
    supervisor.approve_appeal(run_id)
    fake_portal(monkeypatch, clock=clock_after(conn, run_id, 46))

    tracker.track(conn, run_id)
    second = tracker.track(conn, run_id)

    assert second["appeal"] is None
    assert len(draft_rows(conn, run_id, "appeal_second")) == 1
    assert db.get_run(conn, run_id)["status"] == "second_appeal_ready"


def test_an_evasive_reply_becomes_a_section_18_complaint(conn, monkeypatch):
    run_id = seed_first_appeal(conn)
    supervisor.approve_appeal(run_id)
    fake_portal(
        monkeypatch, clock=clock_after(conn, run_id, 46),
        replies=[{"evasive": True, "text": "See attached."}],
    )

    tracker.track(conn, run_id)

    complaints = draft_rows(conn, run_id, "complaint")
    assert len(complaints) == 1
    assert "Section 18" in complaints[0]["text"]
    assert "Section 20" in complaints[0]["text"]
    assert db.get_run(conn, run_id)["status"] == "complaint_ready"
    assert db.get_tracker(conn, run_id)["complaint_draft_id"] == complaints[0]["draft_id"]
    assert draft_rows(conn, run_id, "appeal_second") == []


def test_approving_the_second_appeal_closes_the_run(conn, monkeypatch):
    run_id = seed_first_appeal(conn)
    supervisor.approve_appeal(run_id)
    fake_portal(monkeypatch, clock=clock_after(conn, run_id, 46))
    tracker.track(conn, run_id)

    detail = supervisor.approve_appeal(run_id)

    assert detail["run"]["status"] == "closed"
    assert db.current_approval(conn, run_id)["status"] == "approved"
