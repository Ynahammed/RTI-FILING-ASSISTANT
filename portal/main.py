"""Mock RTI portal HTTP application.

Run: uvicorn portal.main:app --port 8001
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from common import config
from common.crypto import verify_approval
from portal import db, ui

app = FastAPI(title="Mock RTI Portal", docs_url="/api/docs", redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def check_admin(token: str | None) -> None:
    if not token or token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="demo operator token required")


def _fmt_deadline(app_row: Any) -> str:
    raw = app_row["displayed_deadline"] or app_row["deadline"] or ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if app_row["urgent"]:
        return dt.strftime("%d %b %Y, %H:%M IST")
    return dt.strftime("%d %b %Y")


def _get_app(app_id: str) -> Any:
    conn = db.connect()
    row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="application not found")
    return row


def _process_payment(
    conn: Any, app_row: Any, *, approval_token: str, idempotency_key: str, amount: int
) -> dict[str, Any]:
    payload = verify_approval(approval_token) if approval_token else None
    return db.record_payment(
        conn,
        app_row,
        idempotency_key=idempotency_key,
        amount_inr=amount,
        token_payload=payload,
        token_present=bool(approval_token),
    )


# ---------------------------------------------------------------- citizen pages


@app.get("/")
def home() -> Any:
    conn = db.connect()
    apps = list(conn.execute("SELECT * FROM applications ORDER BY created_at DESC LIMIT 25"))
    now = db.iso(db.mock_now(conn))
    fee = int(db.get_setting(conn, "fee_inr", "10"))
    conn.close()
    return ui.home(now, apps, fee)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "mock-portal"}


@app.get("/apply")
def apply_form() -> Any:
    conn = db.connect()
    auths = db.all_authorities(conn)
    conn.close()
    return ui.apply_form(auths)


@app.post("/apply")
def apply_submit(
    applicant_name: str = Form(""),
    applicant_address: str = Form(""),
    state: str = Form(""),
    department: str = Form(""),
    authority_code: str = Form(""),
    information_sought: str = Form(""),
    bpl: str | None = Form(None),
    urgent: str | None = Form(None),
    idempotency_key: str = Form(""),
) -> Any:
    conn = db.connect()
    try:
        auths = db.all_authorities(conn)
        values = {
            "applicant_name": applicant_name, "applicant_address": applicant_address,
            "state": state, "department": department, "authority_code": authority_code,
            "information_sought": information_sought, "bpl": bool(bpl), "urgent": bool(urgent),
            "idempotency_key": idempotency_key,
        }
        errors = []
        if not applicant_name.strip():
            errors.append("Applicant name is required.")
        if not information_sought.strip():
            errors.append("Information sought is required.")
        if not db.authority(conn, authority_code):
            errors.append("Select a public authority from the list.")
        if not state.strip():
            errors.append("State is required.")
        if errors:
            return ui.apply_form(auths, values, " ".join(errors))
        row, duplicate = db.create_application(
            conn,
            {
                "applicant_name": applicant_name.strip(),
                "applicant_address": applicant_address.strip(),
                "state": state,
                "department": department,
                "authority_code": authority_code,
                "information_sought": information_sought.strip(),
                "bpl": bool(bpl),
                "urgent": bool(urgent),
                "idempotency_key": idempotency_key or f"PORTAL-{uuid.uuid4().hex[:12]}",
            },
        )
        if duplicate:
            return ui.already_received(row)
        return RedirectResponse(f"/captcha/{row['app_id']}", status_code=303)
    finally:
        conn.close()


@app.get("/captcha/{app_id}")
def captcha(app_id: str) -> Any:
    row = _get_app(app_id)
    return ui.captcha_page(row, config.ADMIN_TOKEN)


@app.get("/pay/{app_id}")
def pay_page(app_id: str, error: str = "") -> Any:
    row = _get_app(app_id)
    return ui.pay_page(row, db.fee_for(bool(row["bpl"])), error)


@app.post("/pay/{app_id}")
def pay_submit(
    app_id: str,
    approval_token: str = Form(""),
    idempotency_key: str = Form(""),
    amount: int = Form(0),
) -> Any:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        if row["reg_no"]:
            return RedirectResponse(f"/confirmation/{row['reg_no']}", status_code=303)
        result = _process_payment(
            conn, row, approval_token=approval_token,
            idempotency_key=idempotency_key or f"PAY-{uuid.uuid4().hex[:10]}", amount=amount,
        )
        if not result["ok"]:
            return ui.pay_page(row, db.fee_for(bool(row["bpl"])), result["reason"])
        if result.get("timeout"):
            return JSONResponse({"detail": "timeout", "app_id": app_id}, status_code=504)
        return RedirectResponse(f"/confirmation/{result['reg_no']}", status_code=303)
    finally:
        conn.close()


@app.get("/confirmation/{reg_no:path}")
def confirmation(reg_no: str) -> Any:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE reg_no=?", (reg_no,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="registration number not found")
        if not row["confirmation_available"]:
            return JSONResponse(
                {"detail": "confirmation temporarily unavailable", "reg_no": reg_no}, status_code=503
            )
        auth_row = db.authority(conn, row["authority_code"])
        transfer_row = db.authority(conn, row["transfer_to_code"]) if row["transfer_to_code"] else None
        return ui.confirmation_page(row, auth_row, transfer_row, _fmt_deadline(row))
    finally:
        conn.close()


@app.get("/transfer/{app_id}")
def transfer(app_id: str) -> Any:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        if not row or not row["transfer_to_code"]:
            raise HTTPException(status_code=404, detail="no transfer recorded for this application")
        return ui.transfer_page(
            row, db.authority(conn, row["authority_code"]), db.authority(conn, row["transfer_to_code"])
        )
    finally:
        conn.close()


@app.get("/grievance")
def grievance() -> Any:
    return ui.grievance_page()


@app.post("/grievance")
def grievance_submit(name: str = Form(""), body: str = Form("")) -> Any:
    return ui.grievance_received(name)


# ------------------------------------------------------------------- operator


@app.get("/admin/clock")
def clock_console() -> Any:
    conn = db.connect()
    try:
        apps = list(conn.execute("SELECT * FROM applications ORDER BY created_at DESC LIMIT 40"))
        now = db.iso(db.mock_now(conn))
        timeout = db.get_setting(conn, "timeout_next_filing", "0") == "1"
        skew = int(db.get_setting(conn, "skew_next_deadline_days", "0") or 0)
        return ui.clock_page(now, apps, timeout, skew, config.ADMIN_TOKEN)
    finally:
        conn.close()


@app.post("/admin/clock")
def clock_advance(admin_token: str = Form(""), days: int = Form(0)) -> Any:
    check_admin(admin_token)
    conn = db.connect()
    try:
        db.advance_clock(conn, max(0, days))
    finally:
        conn.close()
    return RedirectResponse("/admin/clock", status_code=303)


@app.post("/admin/simulate")
def simulate(
    admin_token: str = Form(""),
    timeout_next_filing: str | None = Form(None),
    skew_next_deadline_days: int = Form(0),
) -> Any:
    check_admin(admin_token)
    conn = db.connect()
    try:
        db.set_setting(conn, "timeout_next_filing", "1" if timeout_next_filing else "0")
        db.set_setting(conn, "skew_next_deadline_days", str(max(0, skew_next_deadline_days)))
    finally:
        conn.close()
    return RedirectResponse("/admin/clock", status_code=303)


@app.post("/admin/captcha/{app_id}/complete")
def admin_captcha(app_id: str, admin_token: str = Form("")) -> Any:
    check_admin(admin_token)
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        db.captcha_complete(conn, app_id)
    finally:
        conn.close()
    return RedirectResponse(f"/pay/{app_id}", status_code=303)


@app.post("/admin/reply/{app_id}")
def admin_reply(app_id: str, admin_token: str = Form(""), evasive: str | None = Form(None)) -> Any:
    check_admin(admin_token)
    conn = db.connect()
    try:
        body = (
            "The information sought relates to an ongoing matter; no records can be provided at this stage."
            if evasive
            else "Records have been compiled and are available for inspection."
        )
        db.add_reply(conn, app_id, body, bool(evasive))
    finally:
        conn.close()
    return RedirectResponse("/admin/clock", status_code=303)


@app.post("/admin/confirmation/{app_id}/restore")
def admin_restore(app_id: str, admin_token: str = Form("")) -> Any:
    check_admin(admin_token)
    conn = db.connect()
    try:
        conn.execute("UPDATE applications SET confirmation_available=1 WHERE app_id=?", (app_id,))
    finally:
        conn.close()
    return RedirectResponse("/admin/clock", status_code=303)


# ------------------------------------------------------------------------ APIs


@app.get("/api/clock")
def api_clock() -> dict[str, str]:
    conn = db.connect()
    try:
        return {"mock_now": db.iso(db.mock_now(conn))}
    finally:
        conn.close()


@app.get("/api/status/{app_id}")
def api_status(app_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        return {
            "app_id": row["app_id"],
            "status": row["status"],
            "reg_no": row["reg_no"],
            "captcha_passed": bool(row["captcha_passed"]),
            "confirmation_url": f"{config.PORTAL_URL}/confirmation/{row['reg_no']}" if row["reg_no"] else None,
            "transfer_url": f"{config.PORTAL_URL}/transfer/{row['app_id']}" if row["transfer_to_code"] else None,
            "transferred_to": row["transfer_to_code"],
            "confirmation_available": bool(row["confirmation_available"]),
        }
    finally:
        conn.close()


@app.get("/api/applications/by-idempotency/{key}")
def api_by_idempotency(key: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE idempotency_key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="no application for that key")
        return {
            "app_id": row["app_id"],
            "status": row["status"],
            "reg_no": row["reg_no"],
            "confirmation_url": f"{config.PORTAL_URL}/confirmation/{row['reg_no']}" if row["reg_no"] else None,
            "confirmation_available": bool(row["confirmation_available"]),
        }
    finally:
        conn.close()


@app.get("/api/replies/{app_id}")
def api_replies(app_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT reply_id, body, evasive, created_at FROM replies WHERE app_id=? ORDER BY created_at",
            (app_id,),
        ))
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/applications")
def api_applications() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM applications ORDER BY created_at DESC"))
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/apply")
async def api_apply(request: Request) -> JSONResponse:
    payload = await request.json()
    conn = db.connect()
    try:
        if not db.authority(conn, payload.get("authority_code", "")):
            raise HTTPException(status_code=422, detail="unknown authority_code")
        row, duplicate = db.create_application(conn, payload)
        return JSONResponse(
            {
                "app_id": row["app_id"],
                "status": row["status"],
                "duplicate": duplicate,
                "pay_url": f"{config.PORTAL_URL}/pay/{row['app_id']}",
                "captcha_url": f"{config.PORTAL_URL}/captcha/{row['app_id']}",
            },
            status_code=200,
        )
    finally:
        conn.close()


@app.post("/api/pay/{app_id}")
async def api_pay(app_id: str, request: Request) -> JSONResponse:
    payload: dict[str, Any] = await request.json()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM applications WHERE app_id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        result = _process_payment(
            conn,
            row,
            approval_token=str(payload.get("approval_token", "")),
            idempotency_key=str(payload.get("idempotency_key") or f"PAY-{uuid.uuid4().hex[:10]}"),
            amount=int(payload.get("amount_inr", -1)),
        )
        if not result["ok"]:
            return JSONResponse({"ok": False, **result}, status_code=402)
        if result.get("timeout"):
            return JSONResponse(
                {"ok": True, "timeout": True, "app_id": app_id, "reg_no": result["reg_no"]},
                status_code=504,
            )
        return JSONResponse({"ok": True, **result, "confirmation_url": f"/confirmation/{result['reg_no']}"})
    finally:
        conn.close()
