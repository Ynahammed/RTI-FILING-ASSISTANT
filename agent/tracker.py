"""Tracker: follows the deadline, transfers, replies and the appeal ladder.

A filed request is only half the job. The tracker follows the response period,
the Section 6(3) transfer and the reply, drafts a Section 19(1) first appeal when
nothing arrives, and — once the first appeal is on record — watches the
Commission's forty-five day window under Section 19(6). If that window passes it
drafts a Section 19(3) second appeal (or a Section 18 complaint when the reply was
evasive rather than absent). Every draft waits for a human approval.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from agent import db, registry, rules
from common import config


def _get_json(path: str) -> dict[str, Any] | list[Any] | None:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{config.PORTAL_URL}{path}")
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _appeal_text(reg_no: str, authority_name: str, due: str, reason: str) -> str:
    return "\n".join([
        f"To: The First Appellate Authority, {authority_name}",
        "",
        f"Subject: First appeal under Section 19 of the Right to Information Act, 2005 — "
        f"application {reg_no}",
        "",
        f"1. I filed an application for information on the matter recorded under registration number {reg_no}.",
        f"2. The response was due on {due}. {reason}",
        "3. Grounds: the Public Information Officer has not supplied the records requested, and the "
        "response period under Section 7(1) has expired.",
        "4. Relief sought: direct the Public Information Officer to supply the requested records within "
        "the time fixed by the Appellate Authority, and to treat the delay as a violation of the Act.",
        "",
        "Place: (synthetic demo)",
        "Applicant: Synthetic Applicant",
    ])


def track(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = db.get_run(conn, run_id)
    app_id = run["portal_app_id"]
    if not app_id:
        raise ValueError("run has no portal application to track")

    status_body = _get_json(f"/api/status/{app_id}") or {}
    clock_body = _get_json("/api/clock") or {}
    replies = _get_json(f"/api/replies/{app_id}") or []

    tracker = db.get_tracker(conn, run_id)
    due_raw = (tracker["due_date"] if tracker else None) or run["deadline"]
    if not due_raw and status_body.get("reg_no"):
        # Fall back to the confirmation page's own filing date via the verifier.
        verification = db.latest_verification(conn, run_id)
        if verification and verification["observed_deadline"]:
            due_raw = verification["observed_deadline"]

    now = None
    if clock_body.get("mock_now"):
        try:
            now = datetime.fromisoformat(clock_body["mock_now"])
        except ValueError:
            now = None
    due = None
    if due_raw:
        try:
            due = datetime.fromisoformat(due_raw)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        except ValueError:
            due = None

    transferred_to = status_body.get("transferred_to")
    if transferred_to:
        db.upsert_tracker(conn, run_id, transferred_to=transferred_to)
        existing = conn.execute(
            "SELECT 1 FROM decisions WHERE run_id=? AND agent='tracker' AND label='transferred'", (run_id,)
        ).fetchone()
        if not existing:
            target = registry.by_code(transferred_to)
            db.add_decision(
                conn, run_id, "tracker", "transferred", 0.95,
                f"Portal transferred the application under Section 6(3) to "
                f"{target['name'] if target else transferred_to}. The tracker now follows that PIO.",
                "6(3)", {"transferred_to": transferred_to},
            )
            db.add_evidence(
                conn, run_id, source_id="TRANSFER-1", source_url=f"{config.PORTAL_URL}/transfer/{app_id}",
                extracted_value=f"Section 6(3) transfer to {transferred_to}",
                source="portal", kind="transfer",
            )

    evasive = any(bool(r.get("evasive")) for r in replies if isinstance(r, dict))
    if evasive:
        reply_state = "evasive"
    elif replies:
        reply_state = "replied"
    elif due and now and now > due:
        reply_state = "no_reply"
    else:
        reply_state = "awaiting"

    db.upsert_tracker(
        conn, run_id,
        reg_no=status_body.get("reg_no") or run["reg_no"],
        due_date=due_raw,
        reply_state=reply_state,
    )

    tracker = db.get_tracker(conn, run_id)
    if tracker and tracker["appeal_filed_at"]:
        # The first appeal is already with the appellate authority. Follow the
        # Commission's clock instead of drafting a first appeal a second time.
        return _follow_filed_appeal(
            conn, run_id, tracker, reply_state, due_raw, status_body, now, transferred_to
        )

    needs_appeal = reply_state in {"no_reply", "evasive"}
    if needs_appeal and not (tracker and tracker["appeal_draft_id"]):
        appeal = _draft_appeal(conn, run_id, reply_state, due_raw, status_body)
        db.set_status(conn, run_id, "appeal_ready")
        return {
            "reply_state": reply_state, "appeal": appeal,
            "transferred_to": transferred_to, "ladder": "first",
        }

    if needs_appeal:
        db.set_status(conn, run_id, "appeal_ready")
    else:
        db.set_status(conn, run_id, "tracking")
    return {
        "reply_state": reply_state, "appeal": None,
        "transferred_to": transferred_to, "ladder": None,
    }


def _draft_appeal(
    conn: sqlite3.Connection, run_id: str, reply_state: str, due: str | None, status_body: dict[str, Any]
) -> dict[str, Any]:
    run = db.get_run(conn, run_id)
    approval = db.current_approval(conn, run_id)
    destination = approval["destination"] if approval else ""
    authority = registry.by_code(destination)
    authority_name = authority["name"] if authority else destination
    reg_no = status_body.get("reg_no") or run["reg_no"] or "unknown"
    reason = (
        "No reply has been received."
        if reply_state == "no_reply"
        else "The reply received did not supply the records requested and appears evasive."
    )
    text = _appeal_text(reg_no, authority_name, due or "the due date", reason)
    draft_id = db.add_draft(
        conn, run_id, kind="appeal_first",
        subject=f"First appeal under Section 19 — {reg_no}",
        text=text,
        rewrite_notes=[
            "Drafted a first appeal because the response period expired without records."
            if reply_state == "no_reply"
            else "Drafted a first appeal because the reply was evasive and did not supply records.",
            "Cited the response period and asked for a direction to supply the records.",
        ],
    )
    approval_id = db.create_approval(conn, run_id, draft_id, 0, destination)
    db.upsert_tracker(conn, run_id, appeal_draft_id=draft_id)
    db.add_decision(
        conn, run_id, "tracker", "appeal_drafted", 0.9,
        f"{reason} Drafted a Section 19 first appeal for human approval.",
        "19", {"draft_id": draft_id, "approval_id": approval_id, "reply_state": reply_state},
    )
    db.add_evidence(
        conn, run_id, source_id=draft_id, source_url=f"{config.PORTAL_URL}/confirmation/{reg_no}",
        extracted_value=f"First appeal drafted after {reply_state.replace('_', ' ')}",
        source="agent", kind="appeal",
    )
    return {"draft_id": draft_id, "approval_id": approval_id, "text": text}


# --------------------------------------------------------------- appeal ladder


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _second_appeal_text(reg_no: str, due: str, filed_at: str, reason: str) -> str:
    return "\n".join([
        "To: The Information Commission",
        "",
        "Subject: Second appeal under Section 19(3) of the Right to Information Act, 2005 — "
        f"application {reg_no}",
        "",
        "1. I filed an application for information, recorded under registration number "
        f"{reg_no}.",
        f"2. The response was due on {due}. {reason}",
        f"3. I filed a first appeal under Section 19(1) on {filed_at}. The First Appellate "
        "Authority has not decided it within the forty-five days allowed by Section 19(6) of the Act.",
        "4. Grounds: the first appeal has not been decided, and the records requested have "
        "not been supplied.",
        "5. Relief sought: direct the Public Information Officer to supply the requested records "
        "within the time fixed by the Commission, and consider penalty under Section 20(1) of the "
        "Act, which provides for two hundred and fifty rupees for each day of delay, subject to a "
        "cap of twenty-five thousand rupees.",
        "",
        "Place: (synthetic demo)",
        "Applicant: Synthetic Applicant",
    ])


def _complaint_text(reg_no: str, authority_name: str, due: str, reason: str) -> str:
    return "\n".join([
        f"To: The Information Commission (complaint against {authority_name})",
        "",
        "Subject: Complaint under Section 18 of the Right to Information Act, 2005 — "
        f"application {reg_no}",
        "",
        f"1. I filed an application for information, recorded under registration number {reg_no}.",
        f"2. The response was due on {due}. {reason}",
        "3. Grounds: the reply received did not supply the records requested and appears "
        "incomplete or misleading, which is a ground of complaint under Section 18(1) of the Act.",
        "4. Relief sought: inquire into the matter, direct the Public Information Officer to supply "
        "the requested records, and consider action under Section 20(1) of the Act.",
        "",
        "Place: (synthetic demo)",
        "Applicant: Synthetic Applicant",
    ])


def _follow_filed_appeal(
    conn: sqlite3.Connection,
    run_id: str,
    tracker: sqlite3.Row,
    reply_state: str,
    due_raw: str | None,
    status_body: dict[str, Any],
    now: datetime | None,
    transferred_to: str | None,
) -> dict[str, Any]:
    """Escalate after a first appeal: second appeal, or a complaint for a bad reply."""
    moment = now or datetime.now(timezone.utc)
    filed_at = _parse_moment(tracker["appeal_filed_at"])
    window_days = int(rules.section("19(6)").get("decision_days", 45) or 45)

    idle = {
        "reply_state": reply_state, "appeal": None,
        "transferred_to": transferred_to, "ladder": None,
    }
    if filed_at is None or moment < filed_at + timedelta(days=window_days):
        db.set_status(conn, run_id, "appeal_filed")
        return idle

    if tracker["second_appeal_draft_id"] or tracker["complaint_draft_id"]:
        status = "complaint_ready" if tracker["complaint_draft_id"] else "second_appeal_ready"
        db.set_status(conn, run_id, status)
        return idle

    if reply_state == "replied":
        # The appellate authority answered and the reply was not evasive. Nothing to
        # escalate automatically; leave the run tracked for the human to read.
        db.set_status(conn, run_id, "tracking")
        return idle

    appeal = _draft_follow_up(conn, run_id, reply_state, due_raw, status_body, tracker["appeal_filed_at"])
    ladder = "complaint" if reply_state == "evasive" else "second"
    db.set_status(conn, run_id, "complaint_ready" if ladder == "complaint" else "second_appeal_ready")
    return {
        "reply_state": reply_state, "appeal": appeal,
        "transferred_to": transferred_to, "ladder": ladder,
    }


def _draft_follow_up(
    conn: sqlite3.Connection,
    run_id: str,
    reply_state: str,
    due: str | None,
    status_body: dict[str, Any],
    filed_at: str,
) -> dict[str, Any]:
    """Draft a second appeal or a Section 18 complaint, and queue its approval."""
    run = db.get_run(conn, run_id)
    approval = db.current_approval(conn, run_id)
    destination = approval["destination"] if approval else ""
    authority = registry.by_code(destination)
    authority_name = authority["name"] if authority else destination or "the public authority"
    reg_no = status_body.get("reg_no") or run["reg_no"] or "unknown"
    due_display = due or "the due date"
    complaint = reply_state == "evasive"
    reason = (
        "The reply received did not supply the records requested and appears evasive."
        if complaint
        else "No reply has been received."
    )

    if complaint:
        text = _complaint_text(reg_no, authority_name, due_display, reason)
        draft_id = db.add_draft(
            conn, run_id, kind="complaint",
            subject=f"Complaint under Section 18 — {reg_no}",
            text=text,
            rewrite_notes=[
                "Drafted a complaint because the reply was evasive rather than a refusal on record.",
                "Section 18(1) covers a reply that is incomplete or misleading.",
                "Asked the Commission to consider penalty under Section 20(1).",
            ],
        )
        db.upsert_tracker(conn, run_id, complaint_draft_id=draft_id)
        label, rule_ref = "complaint_drafted", "18(1)"
        note = (
            "The first appeal drew a reply that did not supply the records. Drafted a Section 18 "
            "complaint for human approval."
        )
    else:
        text = _second_appeal_text(reg_no, due_display, filed_at, reason)
        draft_id = db.add_draft(
            conn, run_id, kind="appeal_second",
            subject=f"Second appeal under Section 19(3) — {reg_no}",
            text=text,
            rewrite_notes=[
                "Drafted a second appeal because the first appeal was not decided within "
                "forty-five days (Section 19(6)).",
                "Cited the ninety-day window under Section 19(3) and the penalty under Section 20(1).",
            ],
        )
        db.upsert_tracker(conn, run_id, second_appeal_draft_id=draft_id)
        label, rule_ref = "second_appeal_drafted", "19(3)"
        note = (
            "The first appeal was not decided within forty-five days. Drafted a Section 19(3) "
            "second appeal for human approval."
        )

    approval_id = db.create_approval(conn, run_id, draft_id, 0, destination)
    db.add_decision(
        conn, run_id, "tracker", label, 0.9, note, rule_ref,
        {"draft_id": draft_id, "approval_id": approval_id, "reply_state": reply_state},
    )
    db.add_evidence(
        conn, run_id, source_id=draft_id,
        source_url=f"{config.PORTAL_URL}/confirmation/{reg_no}",
        extracted_value=(
            f"Section 18 complaint drafted after {reply_state.replace('_', ' ')}"
            if complaint
            else f"Second appeal drafted after {reply_state.replace('_', ' ')}"
        ),
        source="agent", kind="complaint" if complaint else "appeal",
    )
    return {"draft_id": draft_id, "approval_id": approval_id, "text": text}
