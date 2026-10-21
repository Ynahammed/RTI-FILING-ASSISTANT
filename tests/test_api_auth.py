"""Access control on the agent HTTP surface.

These tests assert the delivered behaviour, not the helper functions: every
operator route must refuse an anonymous caller, and a run must be readable by the
client that created it and nobody else.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import auth, db
from agent.main import app
from common import config

SAMPLE = (
    "How much did the Public Works Department spend repairing the road in Ward 12, "
    "Pune, during 2025-26?"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DB", tmp_path / "agent-auth.db")
    auth.reset_rate_limits()
    with TestClient(app) as test_client:
        yield test_client
    auth.reset_rate_limits()


@pytest.fixture()
def made(client):
    response = client.post("/api/runs", json={"text": SAMPLE})
    assert response.status_code == 200, response.text
    body = response.json()
    return body["run"]["run_id"], body["run_token"]


def operator() -> dict[str, str]:
    return {"X-Operator-Token": config.OPERATOR_TOKEN}


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/runs"),
        ("get", "/api/review-queue"),
        ("get", "/api/tests"),
        ("post", "/api/tests"),
        ("get", "/api/tracker"),
        ("get", "/api/portal-clock"),
    ],
)
def test_operator_routes_refuse_anonymous_callers(client, method, path):
    response = (
        client.post(path, json={}) if method == "post" else client.get(path)
    )
    assert response.status_code == 401
    assert "operator token" in response.json()["detail"]


def test_operator_routes_accept_the_configured_token(client):
    assert client.get("/api/runs", headers=operator()).status_code == 200
    assert client.get("/api/review-queue", headers=operator()).status_code == 200
    assert client.get("/api/tests", headers=operator()).status_code == 200


def test_bearer_scheme_also_works(client):
    headers = {"Authorization": f"Bearer {config.OPERATOR_TOKEN}"}
    assert client.get("/api/runs", headers=headers).status_code == 200


def test_creating_a_run_does_not_need_a_credential(client):
    response = client.post("/api/runs", json={"text": SAMPLE})
    assert response.status_code == 200
    body = response.json()
    assert body["run_token"]
    assert body["run"]["run_id"]


def test_run_token_reads_only_its_own_run(client, made):
    run_id, token = made
    other = client.post("/api/runs", json={"text": SAMPLE}).json()

    assert client.get(f"/api/runs/{run_id}", headers={"X-Run-Token": token}).status_code == 200
    denied = client.get(
        f"/api/runs/{run_id}", headers={"X-Run-Token": other["run_token"]}
    )
    assert denied.status_code == 403

    anonymous = client.get(f"/api/runs/{run_id}")
    assert anonymous.status_code == 403


def test_unknown_run_is_404_not_a_token_oracle(client):
    response = client.get("/api/runs/RUN-DOES-NOT-EXIST", headers=operator())
    assert response.status_code == 404


def test_run_token_can_answer_and_decline_its_own_run(client, made):
    run_id, token = made
    headers = {"X-Run-Token": token}

    detail = client.get(f"/api/runs/{run_id}", headers=headers).json()
    if detail["run"]["status"] == "awaiting_input":
        answered = client.post(
            f"/api/runs/{run_id}/answer",
            json={"text": "Pune, public works"},
            headers=headers,
        )
        assert answered.status_code == 200
        detail = answered.json()

    if detail["run"]["status"] == "awaiting_approval":
        declined = client.post(
            f"/api/runs/{run_id}/decline", json={"reason": "not mine"}, headers=headers
        )
        assert declined.status_code == 200
        assert declined.json()["run"]["status"] == "declined"


def test_a_foreign_run_token_cannot_answer_or_decline(client, made):
    run_id, _ = made
    other = client.post("/api/runs", json={"text": SAMPLE}).json()["run_token"]
    wrong = {"X-Run-Token": other}

    assert client.post(
        f"/api/runs/{run_id}/answer", json={"text": "Pune"}, headers=wrong
    ).status_code == 403
    assert client.post(
        f"/api/runs/{run_id}/decline", json={"reason": "x"}, headers=wrong
    ).status_code == 403


def test_captcha_handoff_is_operator_only(client, made):
    run_id, token = made
    response = client.post(
        f"/api/runs/{run_id}/captcha-complete", headers={"X-Run-Token": token}
    )
    assert response.status_code == 401


def test_run_tokens_are_stored_hashed(client, made):
    run_id, token = made
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT token_hash FROM run_tokens WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["token_hash"] != token
    assert token not in row["token_hash"]


def test_run_creation_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "RUN_RATE_LIMIT", 2)
    auth.reset_rate_limits()
    statuses = [
        client.post("/api/runs", json={"text": SAMPLE}).status_code for _ in range(3)
    ]
    assert statuses[:2] == [200, 200]
    assert statuses[2] == 429


def test_rate_limit_does_not_block_the_operator(client, monkeypatch):
    monkeypatch.setattr(config, "RUN_RATE_LIMIT", 1)
    auth.reset_rate_limits()
    client.post("/api/runs", json={"text": SAMPLE})
    assert client.get("/api/runs", headers=operator()).status_code == 200
