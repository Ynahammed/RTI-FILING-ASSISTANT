"""Filing client: submits an approved draft through a portal adapter.

The client never pays or submits without an approved approval row: the action
insert itself is guarded by a database trigger. Every call carries a
deterministic idempotency key so a retry or double click cannot file twice.

Transport lives in `agent.portal_adapter`: HTTP for the hosted mock, Playwright
for the real form, or a fixture for tests.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent import db, portal_adapter, registry
from common import config
from common.crypto import idempotency_key

FilingError = portal_adapter.PortalError


def _fields(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    rows = conn.execute("SELECT name, value FROM fields WHERE run_id=?", (run_id,)).fetchall()
    return {r["name"]: r["value"] for r in rows}


def _approval(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    approval = db.current_approval(conn, run_id)
    if not approval:
        raise PermissionError("Blocked: no approval exists for this run.")
    if approval["status"] != "approved":
        raise PermissionError("Blocked: the approval gate has not been passed.")
    return approval


def _submit_payload(
    conn: sqlite3.Connection, run_id: str, draft: sqlite3.Row, approval: sqlite3.Row
) -> dict[str, Any]:
    fields = _fields(conn, run_id)
    # The approved destination is authoritative. Filing the citizen's guessed
    # department with a fallback authority's code produced a state/department/office
    # combination the portal form does not offer, so Playwright waited 30 s for a
    # missing option and fell back to HTTP. Send what the approval actually says.
    authority = registry.by_code(approval["destination"])
    state = authority["state"] if authority else fields.get("state", "")
    department = authority["department"] if authority else fields.get("department", "")
    # The X8 demo files to a deliberately wrong office to exercise the portal's
    # Section 6(3) transfer. That only works if the form still describes the real
    # subject, so the demo override keeps the citizen's department on purpose.
    overrides = json.loads(db.get_run(conn, run_id)["overrides"] or "{}")
    if overrides.get("authority_code") == approval["destination"]:
        department = fields.get("department", department)
    return {
        "applicant_name": fields.get("applicant_name", "Synthetic Applicant"),
        "applicant_address": "Synthetic address for the mock pilot",
        "state": state,
        "department": department,
        "authority_code": approval["destination"],
        "information_sought": draft["text"],
        "bpl": fields.get("bpl") == "true",
        "urgent": fields.get("urgency") == "urgent",
        "idempotency_key": idempotency_key(
            "submit", run_id, draft["draft_id"], approval["destination"]
        ),
    }


def submit_application(
    conn: sqlite3.Connection,
    run_id: str,
    adapter: portal_adapter.PortalAdapter | None = None,
) -> dict[str, Any]:
    """Submit the approved draft to the portal. Stops at the CAPTCHA handoff."""
    adapter = adapter or portal_adapter.get_adapter()
    approval = _approval(conn, run_id)
    draft = db.latest_draft(conn, run_id, "application")
    if not draft:
        raise FilingError("no application draft to file")
    payload = _submit_payload(conn, run_id, draft, approval)
    submit_key = payload["idempotency_key"]

    action, _ = db.record_action(
        conn, run_id, "submit", draft["draft_id"], approval["destination"], submit_key,
        {"payload": payload, "mode": getattr(adapter, "mode", adapter.name)},
        lambda: adapter.submit(payload),
    )
    response: dict[str, Any] = json.loads(action["response"] or "{}")
    if action["state"] != "complete" or not response.get("app_id"):
        # Resolve the application id by idempotency key: a timeout may still have
        # created the application on the portal.
        resolved = adapter.resolve_by_idempotency(submit_key)
        if resolved:
            response = resolved
        else:
            raise FilingError(response.get("error") or "portal submission failed")

    app_id = response["app_id"]
    db.set_run_fields(conn, run_id, portal_app_id=app_id)
    db.add_evidence(
        conn, run_id, source_id=action["action_id"],
        source_url=f"{config.PORTAL_URL}/captcha/{app_id}",
        extracted_value=f"Application submitted to {approval['destination']} via {response.get('transport')}",
        source="portal", kind="submit",
    )
    if response.get("captcha_pending", True):
        db.set_status(conn, run_id, "awaiting_captcha")
    return response


def pay_and_file(
    conn: sqlite3.Connection,
    run_id: str,
    adapter: portal_adapter.PortalAdapter | None = None,
) -> dict[str, Any]:
    """Pay the fee after the CAPTCHA handoff. Never retries a timed-out payment."""
    adapter = adapter or portal_adapter.get_adapter()
    approval = _approval(conn, run_id)
    run = db.get_run(conn, run_id)
    app_id = run["portal_app_id"]
    if not app_id:
        raise FilingError("no portal application to pay for")
    draft = db.latest_draft(conn, run_id, "application")
    if not draft:
        raise FilingError("no draft for this run")

    payment_key = idempotency_key("payment", run_id, draft["draft_id"], approval["destination"])
    token = approval["token"] or ""
    amount = int(approval["fee_inr"])

    action, executed = db.record_action(
        conn, run_id, "payment", draft["draft_id"], approval["destination"], payment_key,
        {"app_id": app_id, "amount_inr": amount, "mode": getattr(adapter, "mode", adapter.name)},
        lambda: adapter.pay(app_id, token, payment_key, amount),
    )
    response: dict[str, Any] = json.loads(action["response"] or "{}")
    if action["state"] != "complete":
        response = {
            "ok": False, "timeout": False,
            "reason": "payment transport failed, not retried", "app_id": app_id,
        }
        db.set_status(conn, run_id, "filed_unverified")
        return response

    if not response.get("ok"):
        db.set_status(conn, run_id, "failed")
        db.add_decision(
            conn, run_id, "filing", "payment_rejected", 0.95,
            f"Portal rejected the payment: {response.get('reason', 'unknown reason')}.",
            "fee", {"action_id": action["action_id"]},
        )
        return response

    if response.get("timeout"):
        # Resolve the registration number through the submission idempotency key,
        # without paying a second time.
        submit_key = idempotency_key("submit", run_id, draft["draft_id"], approval["destination"])
        resolved = adapter.resolve_by_idempotency(submit_key) or {}
        reg_no = resolved.get("reg_no") or response.get("reg_no")
        if reg_no:
            db.set_run_fields(conn, run_id, reg_no=reg_no)
        db.add_decision(
            conn, run_id, "filing", "payment_unknown", 0.6,
            "The payment call timed out before the confirmation page rendered. No retry was attempted; "
            "the registration number was recovered by idempotency lookup.",
            "7(1)", {"action_id": action["action_id"], "reg_no": reg_no},
        )
        db.set_status(conn, run_id, "filed_unverified")
        return {**response, "reg_no": reg_no, "executed": executed}

    reg_no = response.get("reg_no")
    if reg_no:
        db.set_run_fields(conn, run_id, reg_no=reg_no)
    db.add_evidence(
        conn, run_id, source_id=action["action_id"],
        source_url=f"{config.PORTAL_URL}/confirmation/{reg_no}",
        extracted_value=f"Payment accepted (Rs {amount}); registration {reg_no}",
        source="portal", kind="payment",
    )
    db.add_decision(
        conn, run_id, "filing", "filed", 0.95,
        f"Portal accepted the payment and issued registration number {reg_no}.",
        "7(1)", {"action_id": action["action_id"], "reg_no": reg_no},
    )
    db.set_status(conn, run_id, "filed")
    return {**response, "executed": executed}


def operator_complete_captcha(
    app_id: str, adapter: portal_adapter.PortalAdapter | None = None
) -> bool:
    """Demo-operator action. The agent never solves a challenge itself."""
    adapter = adapter or portal_adapter.get_adapter()
    return adapter.complete_captcha(app_id)
