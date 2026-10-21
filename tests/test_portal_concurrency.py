"""Concurrent payments must never receive the same registration number.

The portal allocates a registration number by counting existing applications for
the department and year. Under concurrency two payers could read the same count,
which produced `UNIQUE constraint failed: applications.reg_no` and a 500 on the
payment route. This test pays twelve applications at once and checks the numbers.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import config
from portal import db as portal_db


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PORTAL_DB", tmp_path / "portal-test.db")
    conn = portal_db.init_db()
    yield conn
    conn.close()


def _make_application(conn, index: int):
    authority = conn.execute("SELECT * FROM authorities ORDER BY code LIMIT 1").fetchone()
    app, _ = portal_db.create_application(
        conn,
        {
            "applicant_name": f"Synthetic Applicant {index}",
            "applicant_address": "Synthetic address",
            "state": authority["state"],
            "department": authority["department"],
            "authority_code": authority["code"],
            "information_sought": "Records requested for a concurrency test.",
            "bpl": False,
            "urgent": False,
            "idempotency_key": f"concurrency-submit-{index}",
        },
    )
    portal_db.captcha_complete(conn, app["app_id"])
    return conn.execute("SELECT * FROM applications WHERE app_id=?", (app["app_id"],)).fetchone()


def _pay(app, index: int) -> dict:
    conn = portal_db.connect()
    try:
        return portal_db.record_payment(
            conn,
            app,
            idempotency_key=f"concurrency-pay-{index}",
            amount_inr=10,
            token_payload={
                "destination": app["authority_code"],
                "fee_inr": 10,
                "approval_id": f"APR-{index}",
            },
            token_present=True,
        )
    finally:
        conn.close()


def test_concurrent_payments_get_unique_registration_numbers(portal):
    apps = [_make_application(portal, i) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda pair: _pay(pair[1], pair[0]), enumerate(apps)))

    assert all(result["ok"] for result in results), results
    reg_numbers = [result["reg_no"] for result in results]
    assert len(set(reg_numbers)) == len(reg_numbers)
    stored = [
        row["reg_no"]
        for row in portal.execute(
            "SELECT reg_no FROM applications WHERE reg_no IS NOT NULL"
        )
    ]
    assert sorted(stored) == sorted(reg_numbers)
