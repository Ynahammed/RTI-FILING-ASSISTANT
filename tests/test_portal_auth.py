"""The portal's operator console must not hand out its own token.

The demo console used to render `RTI_ADMIN_TOKEN` into hidden inputs, and the
CAPTCHA page rendered it into a visible field. On a public deployment that meant
anyone who opened either page could drive every operator control: advance the
clock, complete a CAPTCHA, flip failure switches. These tests pin the fix: no page
contains the token, the console requires a login, and the form-token path that
scripts and the agent use still works.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common import config
from portal import db as portal_db
from portal.main import ADMIN_COOKIE
from portal.main import app as portal_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PORTAL_DB", tmp_path / "portal-auth.db")
    monkeypatch.setattr(config, "ENV", "local")
    conn = portal_db.init_db()
    conn.close()
    with TestClient(portal_app) as test_client:
        yield test_client


@pytest.fixture()
def application(client):
    conn = portal_db.connect()
    try:
        authority = conn.execute("SELECT * FROM authorities ORDER BY code LIMIT 1").fetchone()
        row, _ = portal_db.create_application(
            conn,
            {
                "applicant_name": "Synthetic Applicant",
                "applicant_address": "Synthetic address",
                "state": authority["state"],
                "department": authority["department"],
                "authority_code": authority["code"],
                "information_sought": "Records requested for an auth test.",
                "bpl": False,
                "urgent": False,
                "idempotency_key": "auth-test-submit",
            },
        )
        return row["app_id"]
    finally:
        conn.close()


def test_console_asks_for_a_login_instead_of_showing_its_token(client):
    page = client.get("/admin/clock")
    assert page.status_code == 200
    assert config.ADMIN_TOKEN not in page.text
    assert 'action="/admin/login"' in page.text


def test_captcha_page_does_not_render_the_operator_token(client, application):
    page = client.get(f"/captcha/{application}")
    assert page.status_code == 200
    assert config.ADMIN_TOKEN not in page.text


def test_login_rejects_a_wrong_token(client):
    page = client.post("/admin/login", data={"admin_token": "not-the-token"})
    assert page.status_code == 200
    assert ADMIN_COOKIE not in page.cookies


def test_login_sets_a_cookie_and_the_console_loads_without_the_token(client):
    page = client.post("/admin/login", data={"admin_token": config.ADMIN_TOKEN}, follow_redirects=False)
    assert page.status_code == 303
    assert page.cookies.get(ADMIN_COOKIE) == config.ADMIN_TOKEN
    console = client.get("/admin/clock")
    assert console.status_code == 200
    assert "Demo console" in console.text
    assert config.ADMIN_TOKEN not in console.text


def test_cookie_authorises_the_operator_actions(client):
    client.post("/admin/login", data={"admin_token": config.ADMIN_TOKEN}, follow_redirects=False)
    response = client.post("/admin/clock", data={"days": "7"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/clock").json()["mock_now"] != ""


def test_form_token_still_works_for_scripts(client):
    response = client.post(
        "/admin/simulate",
        data={"admin_token": config.ADMIN_TOKEN, "skew_next_deadline_days": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_operator_action_without_a_token_is_rejected(client):
    response = client.post("/admin/clock", data={"days": "7"}, follow_redirects=False)
    assert response.status_code == 403
