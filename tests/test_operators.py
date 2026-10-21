"""Operator accounts: hashed tokens, rotation without a redeploy, and revocation.

The earlier design had one shared token from the environment. That works for a
demo and fails the moment there are two reviewers: no way to tell them apart in an
audit trail, and no way to remove one without rotating the secret for everyone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import auth, db
from agent.main import app
from common import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DB", tmp_path / "agent-operators.db")
    auth.reset_rate_limits()
    with TestClient(app) as test_client:
        yield test_client
    auth.reset_rate_limits()


def test_the_configured_token_becomes_the_first_account(client):
    conn = db.connect()
    try:
        accounts = db.list_operators(conn)
        row = db.operator_for_token(conn, config.OPERATOR_TOKEN)
    finally:
        conn.close()
    assert len(accounts) == 1
    assert accounts[0]["name"] == "operator"
    assert accounts[0]["role"] == "admin"
    assert row is not None


def test_tokens_are_stored_hashed(client):
    conn = db.connect()
    try:
        _, token = db.create_operator(conn, "priya")
        stored = conn.execute(
            "SELECT token_hash FROM operators WHERE name='priya'"
        ).fetchone()["token_hash"]
    finally:
        conn.close()
    assert token not in stored
    assert len(stored) == 64  # sha256 hex


def test_a_new_account_can_use_the_operator_surface(client):
    conn = db.connect()
    try:
        _, token = db.create_operator(conn, "priya")
    finally:
        conn.close()
    headers = {"X-Operator-Token": token}
    assert client.get("/api/runs", headers=headers).status_code == 200
    assert client.get("/api/reviews", headers=headers).status_code == 200


def test_revoking_an_account_stops_its_token(client):
    conn = db.connect()
    try:
        _, token = db.create_operator(conn, "priya")
    finally:
        conn.close()
    assert client.get("/api/runs", headers={"X-Operator-Token": token}).status_code == 200

    conn = db.connect()
    try:
        assert db.revoke_operator(conn, "priya") is True
    finally:
        conn.close()
    assert client.get("/api/runs", headers={"X-Operator-Token": token}).status_code == 401


def test_names_are_unique(client):
    conn = db.connect()
    try:
        db.create_operator(conn, "priya")
        with pytest.raises(ValueError):
            db.create_operator(conn, "priya")
    finally:
        conn.close()


def test_bootstrap_does_not_undo_a_rotation(client):
    """A restart with the old env value must not recreate a revoked account."""
    conn = db.connect()
    try:
        db.bootstrap_operator(conn, config.OPERATOR_TOKEN)
        db.revoke_operator(conn, "operator")
        db.bootstrap_operator(conn, config.OPERATOR_TOKEN)
        active = [row for row in db.list_operators(conn) if not row["revoked_at"]]
    finally:
        conn.close()
    assert active == []
    assert client.get("/api/runs", headers={"X-Operator-Token": config.OPERATOR_TOKEN}).status_code == 401
