"""Supervisor: runs the decision agents in order and stops at every gate.

Flow: intake -> triage (with exemption screen) -> urgency -> routing -> drafting
-> fee -> approval card. Only an approved card unlocks submit and payment.
Filing stops at the portal's CAPTCHA step for a human handoff, then the verifier
runs as a separate step and the tracker takes over.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from agent import agents, db, filing, messages, nlu, rules, tracker, verifier, worker
from common import config

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

TERMINAL = {
    "review_queue", "redirected", "declined", "closed", "appeal_ready",
    "second_appeal_ready", "complaint_ready",
}


def _lock(run_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(run_id, threading.Lock())


def _latest(conn: sqlite3.Connection, run_id: str, agent_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM decisions WHERE run_id=? AND agent=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (run_id, agent_name),
    ).fetchone()


def _message_once(conn: sqlite3.Connection, run_id: str, text: str) -> None:
    row = conn.execute("SELECT 1 FROM messages WHERE run_id=? AND text=?", (run_id, text)).fetchone()
    if not row:
        db.add_message(conn, run_id, "assistant", text)


def _deadline_display(raw: str | None) -> str:
    if not raw:
        return "the due date"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return dt.strftime("%d %b %Y")


def _match_state(answer: str) -> str | None:
    text = nlu.normalise(answer)
    for state, keywords in nlu.STATE_KEYWORDS.items():
        if text == nlu.normalise(state) or any(word in text for word in keywords):
            return state
    return None


def _match_department(answer: str) -> str | None:
    text = nlu.normalise(answer)
    for department, keywords in nlu.DEPARTMENT_KEYWORDS.items():
        spelled = department.replace("_", " ")
        if text in {department, spelled} or any(word in text for word, _ in keywords):
            return department
    return None


def _normalise_answer(field: str, answer: str) -> str | None:
    """Map a free-text follow-up answer onto the value the field expects."""
    text = nlu.normalise(answer)
    if not text:
        return None
    if field == "state":
        return _match_state(answer)
    if field == "department":
        return _match_department(answer) or answer.strip()
    return answer.strip()


def _advance(run_id: str, answer: str | None = None) -> dict[str, Any]:
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] in TERMINAL or run["status"] in {"filed", "filed_unverified", "verified", "tracking", "awaiting_captcha"}:
                return db.run_detail(conn, run_id)

            pending = run["missing_evidence"] if answer else None
            if answer:
                db.add_message(conn, run_id, "user", answer)
            # The answer fills the field its question asked about. Appending it to the
            # request and re-extracting put it in whichever field matched first, which
            # overwrote state when the citizen answered about the department.
            combined = run["raw_input"]

            # 1. Intake -------------------------------------------------------
            if run["status"] in {"intake", "awaiting_input"}:
                # Values the citizen already confirmed survive a re-parse of the original
                # text, which on its own still looks incomplete.
                prior = {
                    row["name"]: row["value"]
                    for row in conn.execute(
                        "SELECT name, value FROM fields WHERE run_id=? AND source='user' "
                        "AND confidence>=0.95", (run_id,)
                    )
                }
                intake = agents.intake(conn, run_id, combined, run["language"])
                parsed = intake["parsed"]
                missing = list(intake["missing"])
                if parsed["injection"]:
                    _message_once(conn, run_id, messages.t("injection", run["language"]))
                for name, value in prior.items():
                    db.add_field(conn, run_id, name, value, 0.95, "user")
                    parsed[name] = value
                    missing = [field for field in missing if field != name]
                if pending == "confirm_authority" and answer:
                    reply = nlu.normalise(answer)
                    candidate = conn.execute(
                        "SELECT value FROM fields WHERE run_id=? AND name='authority_candidate'",
                        (run_id,),
                    ).fetchone()
                    if candidate and reply.startswith(("yes", "y ", "confirm", "correct", "ok")):
                        db.add_field(conn, run_id, "authority_confirmed", candidate["value"], 0.95, "user")
                    else:
                        # A citizen who answers "Madhya Pradesh" instead of "yes" is
                        # correcting the jurisdiction, not ignoring the question. Apply
                        # the correction and route again rather than asking forever.
                        corrected_state = _match_state(answer)
                        corrected_department = (
                            None if corrected_state else _match_department(answer)
                        )
                        if corrected_state or corrected_department:
                            field = "state" if corrected_state else "department"
                            value = corrected_state or corrected_department
                            db.add_field(conn, run_id, field, value, 0.95, "user")
                            parsed[field] = value
                            conn.execute(
                                "DELETE FROM fields WHERE run_id=? AND name='authority_candidate'",
                                (run_id,),
                            )
                            db.set_run_fields(conn, run_id, pending_question=None)
                elif pending == "intent" and answer:
                    # The uncertainty question asks records-or-action; the reply decides it.
                    reply = nlu.normalise(answer)
                    if any(word in reply for word in ("record", "copy", "document", "information", "file")):
                        parsed["records_signal"] = True
                        parsed["action_signal"] = False
                elif pending and answer:
                    value = _normalise_answer(pending, answer)
                    if value:
                        db.add_field(conn, run_id, pending, value, 0.95, "user")
                        parsed[pending] = value
                        missing = [name for name in missing if name != pending]
                if missing:
                    field = missing[0]
                    key = {
                        "information_needed": "ask_information_needed",
                        "state": "ask_state",
                        "department": "ask_department",
                    }[field]
                    question = messages.t(key, run["language"])
                    db.set_run_fields(conn, run_id, pending_question=question)
                    db.set_status(conn, run_id, "awaiting_input", missing=field)
                    _message_once(conn, run_id, question)
                    return db.run_detail(conn, run_id)
                db.set_run_fields(conn, run_id, pending_question=None)
            else:
                parsed = nlu.extract(combined)
                parsed["language"] = run["language"]

            # On a resumed run the ledger's validated fields are authoritative.
            fields = {
                r["name"]: r["value"]
                for r in conn.execute("SELECT name, value FROM fields WHERE run_id=?", (run_id,))
            }
            # A reviewer who released this run lifted the flag. Without this, the
            # deterministic screen would queue it again the moment it resumed.
            released = str(fields.get("review_released", "")).lower() in {"true", "1"}
            for name in ("state", "department", "information_needed", "applicant_name"):
                if fields.get(name):
                    parsed[name] = fields[name]
            parsed["level"] = "central" if parsed.get("state") == "Central" else "state"
            parsed["bpl"] = fields.get("bpl") == "true"
            parsed["clean_text"] = fields.get("information_needed") or parsed.get("clean_text") or combined
            parsed.setdefault("department_score", 3 if parsed.get("department") else 0)
            parsed.setdefault("state_hits", [parsed.get("state") or ""])
            parsed.setdefault("department_hits", [])
            parsed.setdefault("opinion", nlu.has_opinion(combined))
            parsed.setdefault("injection", False)
            parsed.setdefault("personal_data", nlu.has_personal_data(combined))

            # 2. Triage ------------------------------------------------------
            triage_row = _latest(conn, run_id, "triage")
            if triage_row is None or triage_row["label"] == "uncertain":
                triage_result = agents.triage(conn, run_id, parsed)
            else:
                triage_result = {
                    "label": triage_row["label"],
                    "confidence": triage_row["confidence"],
                    "reason": triage_row["reason"],
                    "rule_ref": triage_row["rule_ref"],
                    "decision_id": triage_row["decision_id"],
                }

            if triage_result["label"] == "exempt" and not released:
                db.set_status(conn, run_id, "review_queue")
                db.enqueue_review(
                    conn, run_id,
                    reason="The Section 8(1)(j) rule fired: the request appears to seek another "
                           "person's private information.",
                    source="rule",
                    sla_hours=getattr(config, "REVIEW_SLA_HOURS", 48),
                )
                _message_once(conn, run_id, messages.t("exempt", run["language"]))
                return db.run_detail(conn, run_id)

            if triage_result["label"] == "grievance":
                db.set_status(conn, run_id, "redirected")
                text = messages.t("grievance", run["language"], grievance_url=agents.GRIEVANCE_URL)
                _message_once(conn, run_id, text)
                db.add_evidence(
                    conn, run_id, source_id="GRIEVANCE-REDIRECT", source_url=agents.GRIEVANCE_URL,
                    extracted_value="Citizen redirected to the grievance portal; no RTI application filed.",
                    source="agent", kind="redirect",
                )
                return db.run_detail(conn, run_id)

            if triage_result["label"] == "uncertain":
                question = messages.t("uncertain", run["language"], missing="whether you want records or action")
                db.set_run_fields(conn, run_id, pending_question=question)
                db.set_status(conn, run_id, "awaiting_input", missing="intent")
                _message_once(conn, run_id, question)
                return db.run_detail(conn, run_id)

            if triage_result["label"] == "mixed":
                _message_once(
                    conn, run_id,
                    messages.t("mixed", run["language"], grievance_url=agents.GRIEVANCE_URL),
                )

            # Review routing applies only where a filing would otherwise happen. A
            # grievance is redirected and a blocked exemption is already queued, so
            # queueing those again would add review work with no filing to prevent.
            review = triage_result.get("review_required")
            if review and triage_result["label"] in {"rti", "mixed"} and not released:
                db.set_status(conn, run_id, "review_queue")
                db.enqueue_review(
                    conn, run_id,
                    reason=(
                        "The 8(1)(j) classifier scored this request at or above the review "
                        f"threshold ({review.get('score', 0):.3f} >= {review.get('threshold', 0):.2f})."
                    ),
                    source="classifier",
                    score=review.get("score"),
                    sla_hours=getattr(config, "REVIEW_SLA_HOURS", 48),
                )
                _message_once(conn, run_id, messages.t("review", run["language"]))
                return db.run_detail(conn, run_id)

            # 3. Urgency -----------------------------------------------------
            if _latest(conn, run_id, "urgency") is None:
                agents.urgency(conn, run_id, parsed)
            urgency_row = _latest(conn, run_id, "urgency")
            parsed["urgency"] = bool(urgency_row and urgency_row["label"] == "life_liberty_48h")

            # 4. Routing -----------------------------------------------------
            routing_row = _latest(conn, run_id, "routing")
            if routing_row is None or routing_row["label"] == "UNCERTAIN":
                route = agents.routing(conn, run_id, parsed)
            else:
                output = json.loads(routing_row["output"] or "{}")
                from agent import registry

                authority = registry.by_code(output.get("authority", ""))
                route = {"uncertain": False, "authority": authority, "confidence": routing_row["confidence"]}

            if route.get("uncertain"):
                # A citizen who already confirmed the suggested office goes straight through.
                confirmed = conn.execute(
                    "SELECT value FROM fields WHERE run_id=? AND name='authority_confirmed'",
                    (run_id,),
                ).fetchone()
                candidate = route.get("authority") or {}
                if confirmed and candidate and confirmed["value"] == candidate.get("code"):
                    db.add_decision(
                        conn, run_id, "routing", "routed", 0.9,
                        f"Citizen confirmed {candidate.get('name')} ({candidate.get('code')}) after the "
                        "confidence check, so the recommendation was accepted rather than guessed.",
                        "6(1)", {"authority": candidate.get("code"), "confirmed": True},
                    )
                    route = {"uncertain": False, "authority": candidate, "confidence": 0.9}
                else:
                    question = messages.t("ask_department", run["language"])
                    if route.get("missing") == "state":
                        question = messages.t("ask_state", run["language"])
                    elif route.get("missing") == "confirm_authority" and candidate:
                        db.add_field(conn, run_id, "authority_candidate", candidate.get("code", ""), 0.99, "agent")
                        question = (
                            f"Did you mean {candidate.get('name')} ({candidate.get('code')})? "
                            "Reply yes to confirm."
                        )
                    db.set_run_fields(conn, run_id, pending_question=question)
                    db.set_status(conn, run_id, "awaiting_input", missing=route.get("missing"))
                    _message_once(conn, run_id, question)
                    return db.run_detail(conn, run_id)

            # Demo override used by the evaluation harness (X8): file to the
            # wrong authority on purpose to show the Section 6(3) transfer.
            overrides = json.loads(run["overrides"] or "{}")
            if overrides.get("authority_code"):
                from agent import registry

                forced = registry.by_code(overrides["authority_code"])
                if forced:
                    route["authority"] = forced
                    db.add_decision(
                        conn, run_id, "routing", "overridden", 0.5,
                        f"Demo override: routing forced to {forced['code']} ({forced['name']}) to exercise "
                        "the portal's Section 6(3) transfer path.",
                        "6(3)", {"authority": forced["code"], "demo_override": True},
                    )

            # 5. Drafting ----------------------------------------------------
            draft = db.latest_draft(conn, run_id, "application")
            if draft is None:
                draft_info = agents.drafting(conn, run_id, parsed, route, triage_result)
                draft = db.latest_draft(conn, run_id, "application")
            else:
                draft_info = {"draft_id": draft["draft_id"]}

            # 6. Fee ---------------------------------------------------------
            fee_row = conn.execute(
                "SELECT value FROM fields WHERE run_id=? AND name='fee_inr'", (run_id,)
            ).fetchone()
            if fee_row is None:
                fee_result = agents.fee(conn, run_id, parsed)
            else:
                fee_result = {"fee_inr": int(fee_row["value"])}
                if _latest(conn, run_id, "fee") is None:
                    agents.fee(conn, run_id, parsed)
            parsed["bpl"] = parsed.get("bpl", False)

            # 7. Approval gate ----------------------------------------------
            approval = db.current_approval(conn, run_id)
            if approval is None:
                approval_id = db.create_approval(
                    conn, run_id, draft["draft_id"], fee_result["fee_inr"], route["authority"]["code"],
                )
                approval = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
            db.set_status(conn, run_id, "awaiting_approval")
            _message_once(
                conn, run_id,
                messages.t("approval", run["language"], fee=approval["fee_inr"]),
            )
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


# --------------------------------------------------------------------- public


def start_run(
    text: str, language: str | None = None, overrides: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Create a run and return ``(run_id, run_token)``.

    The token is the client's scoped credential for every later call about this
    run, so it is minted before the response and never stored in plaintext.
    """
    conn = db.connect()
    try:
        db.init_db(conn)
        lang = nlu.detect_language(text, language)
        run_id = db.create_run(conn, language=lang, raw_input=text, overrides=overrides)
        db.add_message(conn, run_id, "user", text)
        run_token = db.issue_run_token(conn, run_id)
    finally:
        conn.close()
    _advance(run_id)
    return run_id, run_token


def answer(run_id: str, text: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        run = db.get_run(conn, run_id)
        if run["status"] != "awaiting_input":
            raise ValueError(f"run is not waiting for an answer (status: {run['status']})")
    finally:
        conn.close()
    return _advance(run_id, answer=text)


def approve(run_id: str) -> dict[str, Any]:
    """Approve the card, then submit. Idempotent: a double click files once."""
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] != "awaiting_approval":
                return db.run_detail(conn, run_id)  # already approved or terminal
            approval = db.approve(conn, run_id)
            db.set_status(conn, run_id, "filing")

            try:
                submit = filing.submit_application(conn, run_id)
            except PermissionError as exc:
                db.set_status(conn, run_id, "failed", missing="approval")
                db.add_decision(
                    conn, run_id, "filing", "blocked", 0.99, str(exc), "gate", {},
                )
                return db.run_detail(conn, run_id)
            except Exception as exc:
                db.set_status(conn, run_id, "failed")
                db.add_decision(
                    conn, run_id, "filing", "submit_failed", 0.9,
                    f"Portal submission failed: {exc}", "6(1)", {"error": str(exc)},
                )
                return db.run_detail(conn, run_id)

            # CAPTCHA handoff is mandatory; the agent never solves it.
            db.set_status(conn, run_id, "awaiting_captcha")
            _message_once(conn, run_id, messages.t("awaiting_captcha", run["language"]))
            db.add_decision(
                conn, run_id, "filing", "captcha_handoff", 0.99,
                "Portal requires a human verification step. The agent stopped and handed off; "
                "no attempt was made to solve it.",
                "human-handoff", {"app_id": submit.get("app_id")},
            )
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


def complete_captcha(run_id: str) -> dict[str, Any]:
    """Demo operator completes the CAPTCHA, then the agent pays and verifies."""
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] != "awaiting_captcha":
                return db.run_detail(conn, run_id)
            app_id = run["portal_app_id"]
            if not app_id or not filing.operator_complete_captcha(app_id):
                db.add_decision(
                    conn, run_id, "filing", "captcha_handoff_failed", 0.7,
                    "The demo operator handoff could not be completed; no payment was attempted.",
                    "human-handoff", {},
                )
                return db.run_detail(conn, run_id)

            db.add_evidence(
                conn, run_id, source_id=app_id, source_url=f"{config.PORTAL_URL}/captcha/{app_id}",
                extracted_value="Demo operator completed the verification step; the agent did not solve it.",
                source="operator", kind="captcha_handoff",
            )
            db.set_status(conn, run_id, "filing")
            payment = filing.pay_and_file(conn, run_id)

            if not payment.get("ok"):
                db.set_status(conn, run_id, "failed")
                _message_once(conn, run_id, messages.t("uncertain", run["language"], missing="a successful payment"))
                return db.run_detail(conn, run_id)

            # A filed request now has a durable follow-up: the worker re-checks it
            # on a schedule instead of waiting for someone to press "Check now".
            worker.enqueue_check(conn, run_id)

            try:
                verification = verifier.verify(conn, run_id)
            except Exception as exc:
                # Payment succeeded, so the run must not sit at `filed` as if the
                # verification had happened. Record the failure and show it.
                db.add_decision(
                    conn, run_id, "verifier", "error", 0.4,
                    f"The verifier could not finish: {type(exc).__name__}: {exc}. "
                    "Recorded as unverified rather than assumed successful.",
                    "7(1)", {"error": str(exc)[:300]},
                )
                db.set_status(conn, run_id, "filed_unverified")
                verification = {"status": "pending", "note": str(exc)}
            if verification.get("status") == "verified":
                _message_once(
                    conn, run_id,
                    messages.t(
                        "filed", run["language"],
                        reg_no=verification.get("observed_reg_no") or run["reg_no"],
                        deadline=_deadline_display(verification.get("observed_deadline")),
                    ),
                )
            else:
                fresh = db.get_run(conn, run_id)
                _message_once(
                    conn, run_id,
                    messages.t(
                        "filed_unverified", run["language"],
                        reg_no=fresh["reg_no"] or verification.get("observed_reg_no") or "pending",
                    ),
                )
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


def decline(run_id: str, reason: str = "") -> dict[str, Any]:
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] != "awaiting_approval":
                return db.run_detail(conn, run_id)
            db.decline(conn, run_id, reason)
            db.set_status(conn, run_id, "declined")
            _message_once(conn, run_id, messages.t("declined", run["language"]))
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


def resume_review(
    run_id: str,
    decision: str,
    operator: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    """Close a human review: uphold the exemption, or release the run to filing.

    Releasing is not a silent override. It records who decided, why, and a field
    the pipeline reads so the same rule cannot queue the run again.
    """
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] != "review_queue":
                raise ValueError("this run is not waiting for review")
            review = db.decide_review(conn, run_id, operator, decision, note)
            name = operator.get("name") or "operator"
            if decision == "uphold":
                db.add_decision(
                    conn, run_id, "review", "exemption_upheld", 1.0,
                    f"Reviewer {name} confirmed the Section 8(1)(j) exemption, so the run is "
                    f"closed. {note.strip() or 'No note was added.'}",
                    "8(1)(j)",
                    {"review_id": review["review_id"], "decided_by": name},
                )
                db.add_evidence(
                    conn, run_id, source_id="REVIEW-DECISION", source_url="",
                    extracted_value=f"Exemption upheld by {name}: {note.strip() or 'no note'}",
                    source="operator", kind="review",
                )
                db.set_status(conn, run_id, "closed")
                _message_once(conn, run_id, messages.t("exempt", run["language"]))
                return db.run_detail(conn, run_id)

            db.add_decision(
                conn, run_id, "review", "released_for_filing", 1.0,
                f"Reviewer {name} released the request for filing. Reason on record: "
                f"{note.strip()}",
                "8(1)(j)",
                {"review_id": review["review_id"], "decided_by": name},
            )
            db.add_evidence(
                conn, run_id, source_id="REVIEW-DECISION", source_url="",
                extracted_value=f"Released by {name}: {note.strip()}",
                source="operator", kind="review",
            )
            db.add_field(conn, run_id, "review_released", "true", 0.99, "operator")
            db.set_status(conn, run_id, "intake")
        finally:
            conn.close()
    # _advance takes the same per-run lock, so it runs after it is released.
    return _advance(run_id)


def track(run_id: str) -> dict[str, Any]:
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if not run["portal_app_id"]:
                raise ValueError("run has not been filed")
            result = tracker.track(conn, run_id)
            # The citizen-facing copy describes the first appeal. A second appeal or
            # complaint is a new card to approve, not a chat message the templates
            # would describe accurately, so it is left to the approval surface.
            if result.get("appeal") and result.get("ladder") in {None, "first"}:
                _message_once(conn, run_id, messages.t("appeal", run["language"]))
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


def approve_appeal(run_id: str) -> dict[str, Any]:
    """Approve the appeal on the card and move the ladder one rung.

    Approving the first appeal does not close the run: the appellate authority now
    has the file, so the tracker watches the Section 19(6) window and escalates if
    it passes. Approving a second appeal or a complaint does close the run.
    """
    with _lock(run_id):
        conn = db.connect()
        try:
            run = db.get_run(conn, run_id)
            if run["status"] not in {"appeal_ready", "second_appeal_ready", "complaint_ready"}:
                return db.run_detail(conn, run_id)
            approval = db.current_approval(conn, run_id)
            if approval and approval["status"] == "pending":
                db.approve(conn, run_id)

            if run["status"] == "appeal_ready":
                filed_at = db.now_iso()
                db.upsert_tracker(conn, run_id, appeal_filed_at=filed_at)
                db.set_status(conn, run_id, "appeal_filed")
                window_days = int(rules.section("19(6)").get("decision_days", 45) or 45)
                worker.enqueue_check(
                    conn, run_id,
                    due_at=datetime.now(timezone.utc) + timedelta(days=window_days),
                )
                db.add_evidence(
                    conn, run_id, source_id="APPEAL-FILED", source_url="",
                    extracted_value=(
                        f"First appeal approved by a human and treated as filed on {filed_at}; "
                        f"the tracker will check the Section 19(6) window of {window_days} days."
                    ),
                    source="approval", kind="appeal",
                )
                return db.run_detail(conn, run_id)

            db.set_status(conn, run_id, "closed")
            return db.run_detail(conn, run_id)
        finally:
            conn.close()


def detail(run_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        return db.run_detail(conn, run_id)
    finally:
        conn.close()
