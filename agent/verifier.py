"""Independent verifier.

It never trusts the filing response. It opens the portal's confirmation page in
a fresh browser (or HTTP client), parses the registration number and deadline,
re-computes the deadline from the portal's own filing date, and only writes a
verification row through the verifier lease.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import httpx

from agent import db, registry, rules
from common import config

REG_RE = re.compile(r"MOCK/[A-Z]+/R/\d{4}/\d{5}")
DEADLINE_RE = re.compile(r"Response deadline</span><strong[^>]*>([^<]+)</strong>")
FILED_RE = re.compile(r"Filed \(mock date\)</th><td>([^<]+)</td>")
AUTH_RE = re.compile(r"Public authority</th><td>([^<]+)</td>")
TRANSFER_RE = re.compile(r'href="(/transfer/[^"]+)"')
TRANSFER_TARGET_RE = re.compile(r"Transferred under Section 6\(3\) to\s*<strong>([^<]+)</strong>")


def _fetch_http(url: str) -> tuple[int, str]:
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        resp = client.get(url)
    return resp.status_code, resp.text


def _fetch_playwright(url: str) -> tuple[int, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()  # a separate context from the filing browser
            response = page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else 0
            return status, page.content()
        finally:
            browser.close()


def fetch_confirmation(url: str) -> tuple[int, str, str]:
    if config.FILING_MODE in {"playwright", "auto"}:
        try:
            status, html = _fetch_playwright(url)
            return status, html, "playwright"
        except Exception:
            if config.FILING_MODE == "playwright":
                raise
    status, html = _fetch_http(url)
    return status, html, "http"


def _parse_display_deadline(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%d %b %Y, %H:%M IST", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def verify(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = db.get_run(conn, run_id)
    app_id = run["portal_app_id"]
    if not app_id:
        raise ValueError("run has no portal application")

    try:
        with httpx.Client(timeout=10) as client:
            status_body = client.get(f"{config.PORTAL_URL}/api/status/{app_id}").json()
    except Exception as exc:
        return _pending(conn, run_id, f"Portal status lookup failed: {exc}")

    reg_no = status_body.get("reg_no")
    if not reg_no:
        return _pending(conn, run_id, "Portal has no registration number for this application yet.")

    url = f"{config.PORTAL_URL}/confirmation/{reg_no}"
    try:
        http_status, page, transport = fetch_confirmation(url)
    except Exception as exc:
        return _pending(conn, run_id, f"Confirmation page could not be opened: {exc}")

    if http_status in (502, 503, 504):
        return _pending(
            conn, run_id,
            f"Confirmation page returned {http_status}; recorded as pending, never as success. "
            "No second payment was made.",
        )
    if http_status == 404:
        verification_id = db.write_verification(
            conn, run_id, status="failed", observed_reg_no=None, observed_deadline=None,
            expected_reg_no=run["reg_no"], expected_deadline=None, matches_intent=False,
            note="Confirmation page returned 404 for the registration number returned by the portal API.",
        )
        db.set_status(conn, run_id, "filed_unverified")
        return {"status": "failed", "verification_id": verification_id, "transport": transport}

    observed_reg = REG_RE.search(page)
    observed_reg_no = observed_reg.group(0) if observed_reg else None
    deadline_match = DEADLINE_RE.search(page)
    observed_deadline_text = deadline_match.group(1).strip() if deadline_match else ""
    observed_deadline = _parse_display_deadline(observed_deadline_text)

    filed_match = FILED_RE.search(page)
    filed_at = None
    if filed_match:
        try:
            filed_at = datetime.fromisoformat(filed_match.group(1).strip())
        except ValueError:
            filed_at = None

    auth_match = AUTH_RE.search(page)
    authority_name = auth_match.group(1).strip() if auth_match else ""
    intended = registry.by_code(db.current_approval(conn, run_id)["destination"]) if db.current_approval(conn, run_id) else None
    intended_name = intended["name"] if intended else ""

    urgency = conn.execute("SELECT value FROM fields WHERE run_id=? AND name='urgency'", (run_id,)).fetchone()
    urgent = bool(urgency and urgency["value"] == "urgent")
    policy = rules.section("7(1)")
    delta = timedelta(hours=policy.get("life_liberty_hours", 48)) if urgent else timedelta(days=policy.get("normal_days", 30))

    expected_deadline = filed_at + delta if filed_at else None
    deadline_ok = False
    if observed_deadline and expected_deadline:
        if urgent:
            deadline_ok = observed_deadline.replace(second=0, microsecond=0) == expected_deadline.replace(
                tzinfo=None, second=0, microsecond=0
            )
        else:
            deadline_ok = observed_deadline.date() == expected_deadline.date()

    reg_ok = bool(observed_reg_no and observed_reg_no == reg_no)
    authority_ok = bool(authority_name and intended_name and authority_name == intended_name)
    transferred = bool(TRANSFER_RE.search(page))
    matches = reg_ok and deadline_ok and authority_ok

    notes = []
    if not reg_ok:
        notes.append("Registration number on the page does not match the portal record.")
    if observed_deadline and not deadline_ok:
        notes.append(
            f"Deadline on the page ({observed_deadline_text}) does not match the policy computed from the "
            f"portal's filing date ({expected_deadline.isoformat() if expected_deadline else 'unknown'})."
        )
    if not observed_deadline:
        notes.append("No deadline could be read from the confirmation page.")
    if not authority_ok:
        notes.append("Authority name on the page did not match the intended authority exactly.")
    if transferred:
        target = TRANSFER_TARGET_RE.search(page)
        notes.append(
            "Portal shows a Section 6(3) transfer"
            + (f" to {target.group(1).strip()}" if target else "")
            + "."
        )

    verification_id = db.write_verification(
        conn, run_id,
        status="verified" if matches else "mismatch",
        observed_reg_no=observed_reg_no,
        observed_deadline=observed_deadline.isoformat() if observed_deadline else None,
        expected_reg_no=reg_no,
        expected_deadline=expected_deadline.isoformat() if expected_deadline else None,
        matches_intent=matches,
        note=" ".join(notes) if notes else "Registration number and deadline match the policy and the portal record.",
    )

    if matches:
        db.set_status(conn, run_id, "verified")
    else:
        db.set_status(conn, run_id, "filed_unverified")

    db.upsert_tracker(
        conn, run_id,
        reg_no=reg_no,
        due_date=observed_deadline.isoformat() if observed_deadline else (
            expected_deadline.isoformat() if expected_deadline else None
        ),
        reply_state="awaiting",
    )
    db.add_evidence(
        conn, run_id, source_id=verification_id,
        source_url=url,
        extracted_value=(
            f"Verifier observed {observed_reg_no or 'no registration number'}; deadline "
            f"{observed_deadline.isoformat() if observed_deadline else 'unreadable'}; matches={matches}"
        ),
        source="verifier", kind="verification",
    )
    db.add_decision(
        conn, run_id, "verifier", "verified" if matches else "mismatch", 0.95 if matches else 0.9,
        "Re-opened the portal page independently. " + ("Observed values match the intent." if matches else " ".join(notes)),
        "7(1)", {
            "verification_id": verification_id,
            "observed_reg_no": observed_reg_no,
            "observed_deadline": observed_deadline.isoformat() if observed_deadline else None,
            "transport": transport,
        },
    )
    return {
        "status": "verified" if matches else "mismatch",
        "verification_id": verification_id,
        "observed_reg_no": observed_reg_no,
        "observed_deadline": observed_deadline.isoformat() if observed_deadline else None,
        "matches_intent": matches,
        "transport": transport,
    }


def _pending(conn: sqlite3.Connection, run_id: str, note: str) -> dict[str, Any]:
    run = db.get_run(conn, run_id)
    verification_id = db.write_verification(
        conn, run_id, status="pending", observed_reg_no=None, observed_deadline=None,
        expected_reg_no=run["reg_no"], expected_deadline=None, matches_intent=False, note=note,
    )
    db.set_status(conn, run_id, "filed_unverified")
    db.add_decision(
        conn, run_id, "verifier", "pending", 0.5,
        note + " Recorded as pending; not claimed as success.",
        "7(1)", {"verification_id": verification_id},
    )
    db.add_evidence(
        conn, run_id, source_id=verification_id,
        source_url=f"{config.PORTAL_URL}/confirmation/{run['reg_no'] or ''}",
        extracted_value=note, source="verifier", kind="verification",
    )
    return {"status": "pending", "verification_id": verification_id, "matches_intent": False}
