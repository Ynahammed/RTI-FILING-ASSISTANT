"""A flagged request is work with an owner and a deadline, not a status.

These tests drive the delivered HTTP surface: a run that the Section 8(1)(j) rule
stops must appear in the queue with a due time, must be claimable by one operator,
and can only be closed by the operator holding it. Releasing it puts it back into
filing with the reviewer's name and reason on the record.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import auth, db
from agent.main import app
from common import config

EXEMPT_TEXT = (
    "Please give me the home address and phone number of the officer in charge of "
    "Ward 12, Pune."
)
# Colloquial shape the appellate-tuned vocabulary missed: no "personal details", a
# role noun (student), and a plural record (defaulters). It must still stop at review.
CITIZEN_EXEMPT_TEXT = (
    "Please give the fee defaulters list with student names and pending amounts "
    "for the municipal school in Ward 12, Pune."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DB", tmp_path / "agent-review.db")
    auth.reset_rate_limits()
    with TestClient(app) as test_client:
        yield test_client
    auth.reset_rate_limits()


def operator(name: str | None = None) -> dict[str, str]:
    if name is None:
        return {"X-Operator-Token": config.OPERATOR_TOKEN}
    conn = db.connect()
    try:
        _, token = db.create_operator(conn, name)
    finally:
        conn.close()
    return {"X-Operator-Token": token}


@pytest.fixture()
def flagged(client):
    response = client.post("/api/runs", json={"text": EXEMPT_TEXT})
    assert response.status_code == 200, response.text
    run_id = response.json()["run"]["run_id"]
    assert response.json()["run"]["status"] == "review_queue"
    return run_id


def test_citizen_phrasing_is_flagged_by_the_rule(client):
    response = client.post("/api/runs", json={"text": CITIZEN_EXEMPT_TEXT})
    assert response.status_code == 200, response.text
    body = response.json()
    run = body["run"]
    assert run["status"] == "review_queue"
    detail = client.get(
        f"/api/runs/{run['run_id']}", headers={"X-Run-Token": body["run_token"]}
    ).json()
    assert any("human review" in m["text"] for m in detail["messages"])
    assert detail["approval"] is None
    assert detail["actions"] == []
    conn = db.connect()
    try:
        review = db.get_review(conn, run["run_id"])
    finally:
        conn.close()
    assert review["source"] == "rule"


def test_a_flagged_run_enters_the_queue_with_a_due_time(client, flagged):
    conn = db.connect()
    try:
        review = db.get_review(conn, flagged)
        counts = db.review_counts(conn)
    finally:
        conn.close()
    assert review is not None
    assert review["source"] == "rule"
    assert review["status"] == "open"
    assert review["due_at"] > review["created_at"]
    assert counts == {"open": 1, "overdue": 0, "unassigned": 1}

    payload = client.get("/api/reviews", headers=operator()).json()
    assert payload["counts"]["open"] == 1
    assert payload["reviews"][0]["run_id"] == flagged
    assert payload["reviews"][0]["assigned_to"] is None


def test_review_routes_are_operator_only(client, flagged):
    assert client.get("/api/reviews").status_code == 401
    assert client.post(f"/api/reviews/{flagged}/claim").status_code == 401
    assert client.post(
        f"/api/reviews/{flagged}/decide", json={"decision": "uphold", "note": ""}
    ).status_code == 401
    assert client.get("/api/audit").status_code == 401


def test_one_operator_cannot_take_anothers_claim(client, flagged):
    first = operator("priya")
    second = operator("arjun")
    assert client.post(f"/api/reviews/{flagged}/claim", headers=first).status_code == 200
    denied = client.post(f"/api/reviews/{flagged}/claim", headers=second)
    assert denied.status_code == 409
    assert "priya" in denied.json()["detail"]


def test_deciding_needs_the_claim(client, flagged):
    response = client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "uphold", "note": ""},
        headers=operator("priya"),
    )
    assert response.status_code == 409
    assert "claim" in response.json()["detail"]


def test_upholding_closes_the_run_and_audits_who_did_it(client, flagged):
    headers = operator("priya")
    client.post(f"/api/reviews/{flagged}/claim", headers=headers)
    detail = client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "uphold", "note": "This is a third party's address."},
        headers=headers,
    ).json()
    assert detail["run"]["status"] == "closed"
    labels = {(d["agent"], d["label"]) for d in detail["decisions"]}
    assert ("review", "exemption_upheld") in labels
    assert any(ev["kind"] == "review" and "priya" in ev["extracted_value"] for ev in detail["evidence"])

    audit = client.get("/api/audit", headers=headers).json()
    actions = {(row["operator_name"], row["action"]) for row in audit}
    assert ("priya", "review.claim") in actions
    assert ("priya", "review.uphold") in actions


def test_release_returns_the_run_to_filing_with_a_reason_on_record(client, flagged):
    headers = operator("priya")
    client.post(f"/api/reviews/{flagged}/claim", headers=headers)
    detail = client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "release", "note": "The public interest in the posting is clear."},
        headers=headers,
    ).json()
    assert detail["run"]["status"] == "awaiting_approval"
    decision = next(d for d in detail["decisions"] if d["agent"] == "review")
    assert decision["label"] == "released_for_filing"
    assert "priya" in decision["reason"]
    assert "public interest" in decision["reason"]
    assert detail["fields"]["review_released"]["value"] == "true"

    conn = db.connect()
    try:
        review = db.get_review(conn, flagged)
    finally:
        conn.close()
    assert review["status"] == "decided"
    assert review["decision"] == "release"
    assert review["decided_by"] == "priya"


def test_release_needs_a_reason(client, flagged):
    headers = operator("priya")
    client.post(f"/api/reviews/{flagged}/claim", headers=headers)
    response = client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "release", "note": "   "},
        headers=headers,
    )
    assert response.status_code == 409
    assert "reason" in response.json()["detail"]


def test_a_decided_review_cannot_be_decided_twice(client, flagged):
    headers = operator("priya")
    client.post(f"/api/reviews/{flagged}/claim", headers=headers)
    client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "uphold", "note": "ok"},
        headers=headers,
    )
    again = client.post(
        f"/api/reviews/{flagged}/decide",
        json={"decision": "release", "note": "changed my mind"},
        headers=headers,
    )
    assert again.status_code == 409


def test_an_overdue_review_is_marked_as_such(client, flagged):
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE reviews SET due_at='2020-01-01T00:00:00+00:00' WHERE run_id=?", (flagged,)
        )
        counts = db.review_counts(conn)
    finally:
        conn.close()
    assert counts["overdue"] == 1
    payload = client.get("/api/reviews", headers=operator()).json()
    assert payload["reviews"][0]["overdue"] is True
