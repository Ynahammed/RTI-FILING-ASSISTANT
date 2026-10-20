"""Filing client: fills the mock portal form with Playwright, HTTP as fallback.

The client never pays or submits without an approved approval row: the action
insert itself is guarded by a database trigger. Every call carries a
deterministic idempotency key so a retry or double click cannot file twice.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import httpx

from agent import db
from common import config
from common.crypto import idempotency_key

CONFIRMATION_RE = re.compile(r"/confirmation/([^\s?#]+)")
APP_ID_RE = re.compile(r"/(?:captcha|pay|confirmation)/([A-Za-z0-9-]+)")


class FilingError(RuntimeError):
    pass


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


def _submit_payload(conn: sqlite3.Connection, run_id: str, draft: sqlite3.Row, approval: sqlite3.Row) -> dict[str, Any]:
    fields = _fields(conn, run_id)
    return {
        "applicant_name": fields.get("applicant_name", "Synthetic Applicant"),
        "applicant_address": "Synthetic address for the mock pilot",
        "state": fields.get("state", ""),
        "department": fields.get("department", ""),
        "authority_code": approval["destination"],
        "information_sought": draft["text"],
        "bpl": fields.get("bpl") == "true",
        "urgent": fields.get("urgency") == "urgent",
        "idempotency_key": idempotency_key(
            "submit", run_id, draft["draft_id"], approval["destination"]
        ),
    }


def _http_submit(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=20) as client:
        resp = client.post(f"{config.PORTAL_URL}/api/apply", json=payload)
    if resp.status_code != 200:
        raise FilingError(f"portal rejected the application: {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    return {
        "app_id": body["app_id"],
        "captcha_pending": True,
        "duplicate": body.get("duplicate", False),
        "transport": "http",
    }


def _playwright_submit(payload: dict[str, Any]) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{config.PORTAL_URL}/apply", wait_until="domcontentloaded")
            page.fill("#applicant_name", payload["applicant_name"])
            page.fill("#applicant_address", payload["applicant_address"])
            page.select_option("#state", payload["state"])
            page.select_option("#department", payload["department"])
            page.select_option("#authority_code", payload["authority_code"])
            page.fill("#information_sought", payload["information_sought"])
            if payload["bpl"]:
                page.check("input[name=bpl]")
            if payload["urgent"]:
                page.check("input[name=urgent]")
            page.evaluate(
                "(value) => { document.querySelector('input[name=idempotency_key]').value = value; }",
                payload["idempotency_key"],
            )
            page.click("button[type=submit]")
            page.wait_for_url(
                re.compile(r"/(captcha|pay|confirmation)/"), timeout=config.PLAYWRIGHT_TIMEOUT_MS
            )
            url = page.url
            alert = ""
            try:
                if page.locator(".alert").count():
                    alert = page.locator(".alert").inner_text()
            except Exception:
                alert = ""
            if alert:
                raise FilingError(f"portal form validation failed: {alert}")
            match = APP_ID_RE.search(url)
            app_id = match.group(1) if match else ""
            return {
                "app_id": app_id,
                "captcha_pending": "/captcha/" in url,
                "url": url,
                "transport": "playwright",
            }
        finally:
            browser.close()


def submit_application(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Submit the approved draft to the portal. Stops at the CAPTCHA handoff."""
    run = db.get_run(conn, run_id)
    approval = _approval(conn, run_id)
    draft = db.latest_draft(conn, run_id, "application")
    if not draft:
        raise FilingError("no application draft to file")
    payload = _submit_payload(conn, run_id, draft, approval)
    submit_key = payload["idempotency_key"]

    mode = config.FILING_MODE

    def perform() -> dict[str, Any]:
        if mode in {"playwright", "auto"}:
            try:
                return _playwright_submit(payload)
            except FilingError:
                raise
            except Exception as exc:
                if mode == "playwright":
                    raise FilingError(f"Playwright filing failed: {exc}") from exc
                response = _http_submit(payload)
                response["transport"] = "http"
                response["fallback_reason"] = str(exc)[:200]
                return response
        return _http_submit(payload)

    action, _ = db.record_action(
        conn, run_id, "submit", draft["draft_id"], approval["destination"], submit_key,
        {"payload": payload, "mode": mode}, perform,
    )
    response: dict[str, Any] = json.loads(action["response"] or "{}")
    if action["state"] != "complete" or not response.get("app_id"):
        # Resolve the application id by idempotency key: a timeout may still have
        # created the application on the portal.
        resolved = _resolve_by_key(submit_key)
        if resolved:
            response = resolved
        else:
            raise FilingError(response.get("error") or "portal submission failed")

    app_id = response["app_id"]
    db.set_run_fields(conn, run_id, portal_app_id=app_id)
    db.add_evidence(
        conn, run_id, source_id=action["action_id"], source_url=f"{config.PORTAL_URL}/captcha/{app_id}",
        extracted_value=f"Application submitted to {approval['destination']} via {response.get('transport')}",
        source="portal", kind="submit",
    )
    if response.get("captcha_pending", True):
        db.set_status(conn, run_id, "awaiting_captcha")
    return response


def _resolve_by_key(key: str) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{config.PORTAL_URL}/api/applications/by-idempotency/{key}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return {
            "app_id": body["app_id"],
            "captcha_pending": body["status"] == "captcha_pending",
            "reg_no": body.get("reg_no"),
            "recovered": True,
        }
    except Exception:
        return None


def _http_pay(app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
    with httpx.Client(timeout=20) as client:
        resp = client.post(
            f"{config.PORTAL_URL}/api/pay/{app_id}",
            json={"approval_token": token, "idempotency_key": key, "amount_inr": amount},
        )
    if resp.status_code == 504:
        return {"ok": True, "timeout": True, "app_id": app_id, "transport": "http"}
    if resp.status_code != 200:
        try:
            reason = resp.json().get("reason", resp.text[:180])
        except Exception:
            reason = resp.text[:180]
        return {"ok": False, "reason": reason, "app_id": app_id, "transport": "http"}
    body = resp.json()
    return {
        "ok": True,
        "timeout": False,
        "app_id": app_id,
        "reg_no": body.get("reg_no"),
        "transport": "http",
        "duplicate": body.get("duplicate", False),
    }


def _playwright_pay(app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{config.PORTAL_URL}/pay/{app_id}", wait_until="domcontentloaded")
            page.fill("#approval_token", token)
            page.fill("#idempotency_key", key)
            page.fill("#amount", str(amount))
            page.click("button[type=submit]")
            try:
                page.wait_for_url(re.compile(r"/confirmation/"), timeout=config.PLAYWRIGHT_TIMEOUT_MS)
            except PWTimeout:
                body = page.inner_text("body")
                lowered = body.lower()
                if "timeout" in lowered or "timed out" in lowered:
                    return {"ok": True, "timeout": True, "app_id": app_id, "transport": "playwright"}
                if page.locator(".alert").count():
                    return {
                        "ok": False,
                        "reason": page.locator(".alert").inner_text(),
                        "app_id": app_id,
                        "transport": "playwright",
                    }
                return {
                    "ok": False,
                    "reason": "payment page did not reach a confirmation",
                    "app_id": app_id,
                    "transport": "playwright",
                }
            match = CONFIRMATION_RE.search(page.url)
            return {
                "ok": True,
                "timeout": False,
                "app_id": app_id,
                "reg_no": match.group(1) if match else None,
                "transport": "playwright",
            }
        finally:
            browser.close()


def pay_and_file(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Pay the fee after the CAPTCHA handoff. Never retries a timed-out payment."""
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
    mode = config.FILING_MODE

    def perform() -> dict[str, Any]:
        if mode in {"playwright", "auto"}:
            try:
                return _playwright_pay(app_id, token, payment_key, amount)
            except Exception as exc:
                if mode == "playwright":
                    raise FilingError(f"Playwright payment failed: {exc}") from exc
                response = _http_pay(app_id, token, payment_key, amount)
                response["fallback_reason"] = str(exc)[:200]
                return response
        return _http_pay(app_id, token, payment_key, amount)

    action, executed = db.record_action(
        conn, run_id, "payment", draft["draft_id"], approval["destination"], payment_key,
        {"app_id": app_id, "amount_inr": amount, "mode": mode}, perform,
    )
    response: dict[str, Any] = json.loads(action["response"] or "{}")
    if action["state"] != "complete":
        response = {"ok": False, "timeout": False, "reason": "payment transport failed, not retried", "app_id": app_id}
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
        resolved = _resolve_by_key(submit_key) or {}
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
        conn, run_id, source_id=action["action_id"], source_url=f"{config.PORTAL_URL}/confirmation/{reg_no}",
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


def operator_complete_captcha(app_id: str) -> bool:
    """Demo-operator action. The agent never solves a challenge itself."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{config.PORTAL_URL}/admin/captcha/{app_id}/complete",
                data={"admin_token": config.ADMIN_TOKEN},
                follow_redirects=False,
            )
        return resp.status_code in {303, 200}
    except Exception:
        return False
