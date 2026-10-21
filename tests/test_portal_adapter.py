"""A second portal implementation, replayed from recorded pages.

This is the point of the adapter seam: filing and verification must work against
something other than the bundled mock. The fixture adapter serves recorded HTML,
so the verifier's parsing and mismatch logic are exercised without a server.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from agent import db, filing, portal_adapter, verifier

REG_NO = "MOCK/PWD/R/2026/00001"
AUTHORITY = "MH-PWD-01"
PAGE = """<html><body>
<table>
<tr><th>Registration</th><td>{reg_no}</td></tr>
<tr><th>Public authority</th><td>Public Works Department, Government of Maharashtra (Pune Division)</td></tr>
<tr><th>Filed (mock date)</th><td>2026-10-20T10:00:00</td></tr>
</table>
<span>Response deadline</span><strong class="due">{deadline}</strong>
</body></html>"""


def fixture(root, *, deadline: str = "19 Nov 2026", status: dict | None = None) -> portal_adapter.FixtureAdapter:
    (root / "status.json").write_text(
        json.dumps(status or {"APP-FIX": {"reg_no": REG_NO}}), encoding="utf-8"
    )
    (root / "submit.json").write_text(
        json.dumps({"app_id": "APP-FIX", "captcha_pending": True}), encoding="utf-8"
    )
    (root / "pay.json").write_text(
        json.dumps({"ok": True, "timeout": False, "reg_no": REG_NO}), encoding="utf-8"
    )
    (root / f"confirmation_{REG_NO.replace('/', '_')}.html").write_text(
        PAGE.format(reg_no=REG_NO, deadline=deadline), encoding="utf-8"
    )
    return portal_adapter.FixtureAdapter(root, app_id="APP-FIX")


def make_approved_run(conn, *, filed: bool = False) -> str:
    run_id = db.create_run(conn, language="en", raw_input="Records request")
    draft_id = db.add_draft(
        conn, run_id, kind="application", subject="Records",
        text="Please provide the work orders and vouchers.",
        rewrite_notes=[],
    )
    db.create_approval(conn, run_id, draft_id, 10, AUTHORITY)
    db.approve(conn, run_id)
    db.add_field(conn, run_id, "urgency", "standard", 0.9, "inferred")
    db.add_field(conn, run_id, "state", "Maharashtra", 0.9, "user")
    db.add_field(conn, run_id, "department", "public_works", 0.9, "user")
    if filed:
        db.set_run_fields(conn, run_id, portal_app_id="APP-FIX", reg_no=REG_NO)
    return run_id


def test_adapter_surface_is_uniform():
    expected = {
        "submit", "pay", "confirmation", "status", "complete_captcha", "resolve_by_idempotency",
    }
    for adapter in (
        portal_adapter.HttpAdapter(base_url="http://127.0.0.1:9"),
        portal_adapter.FixtureAdapter("."),
    ):
        assert expected <= set(dir(adapter))


def test_submit_through_a_recorded_portal(conn, tmp_path):
    adapter = fixture(tmp_path)
    run_id = make_approved_run(conn)
    result = filing.submit_application(conn, run_id, adapter=adapter)

    assert result["app_id"] == "APP-FIX"
    assert result["transport"] == "fixture"
    run = db.get_run(conn, run_id)
    assert run["portal_app_id"] == "APP-FIX"
    assert run["status"] == "awaiting_captcha"
    assert conn.execute("SELECT COUNT(*) AS n FROM actions WHERE type='submit'").fetchone()["n"] == 1


def test_payment_and_filing_through_a_recorded_portal(conn, tmp_path):
    adapter = fixture(tmp_path)
    run_id = make_approved_run(conn)
    filing.submit_application(conn, run_id, adapter=adapter)
    result = filing.pay_and_file(conn, run_id, adapter=adapter)

    assert result["ok"] is True
    run = db.get_run(conn, run_id)
    assert run["reg_no"] == REG_NO
    assert run["status"] == "filed"


def test_verifier_accepts_a_matching_confirmation_page(conn, tmp_path):
    adapter = fixture(tmp_path)
    run_id = make_approved_run(conn, filed=True)
    result = verifier.verify(conn, run_id, adapter=adapter)

    assert result["status"] == "verified"
    assert result["observed_reg_no"] == REG_NO
    assert datetime.fromisoformat(result["observed_deadline"]).date().isoformat() == "2026-11-19"
    assert db.get_run(conn, run_id)["status"] == "verified"


def test_verifier_rejects_a_wrong_deadline(conn, tmp_path):
    adapter = fixture(tmp_path, deadline="25 Nov 2026")
    run_id = make_approved_run(conn, filed=True)
    result = verifier.verify(conn, run_id, adapter=adapter)

    assert result["status"] == "mismatch"
    assert db.get_run(conn, run_id)["status"] == "filed_unverified"
    note = conn.execute("SELECT note FROM verifications").fetchone()["note"]
    assert "Deadline on the page" in note


def test_verifier_treats_a_missing_page_as_a_failure_not_success(conn, tmp_path):
    adapter = fixture(tmp_path, status={"APP-FIX": {"reg_no": "MOCK/PWD/R/2026/99999"}})
    run_id = make_approved_run(conn, filed=True)
    result = verifier.verify(conn, run_id, adapter=adapter)

    # A 404 is not a pending page: the portal claims a registration number it
    # cannot show, so the verification fails rather than waiting indefinitely.
    assert result["status"] == "failed"
    assert db.get_run(conn, run_id)["status"] == "filed_unverified"
    assert conn.execute("SELECT verification_status FROM verifications").fetchone()[0] == "failed"


def test_validation_refusal_from_the_browser_is_not_masked(monkeypatch):
    """A form rejection must surface; falling back to the API would hide it."""
    adapter = portal_adapter.AutoAdapter(mode="auto")
    assert adapter.playwright is not None
    monkeypatch.setattr(
        adapter.playwright, "submit",
        lambda payload: (_ for _ in ()).throw(portal_adapter.PortalError("validation failed")),
    )
    called: list[dict] = []
    monkeypatch.setattr(adapter.http, "submit", lambda payload: called.append(payload) or {})

    with pytest.raises(portal_adapter.PortalError, match="validation failed"):
        adapter.submit({"idempotency_key": "k"})
    assert called == []
