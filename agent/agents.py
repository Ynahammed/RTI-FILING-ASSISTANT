"""Decision agents: intake, triage, exemption screen, urgency, routing, drafting, fee.

Each function writes its decision and evidence rows and returns the structured
result the supervisor needs. None of them performs an irreversible action.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from agent import db, llm, nlu, registry, rules
from common import config

GRIEVANCE_URL = f"{config.PORTAL_URL}/grievance"
DEPARTMENT_LABELS = registry.DEPARTMENT_LABELS


def _fmt_confidence(value: float) -> float:
    return round(max(0.0, min(0.97, value)), 2)


# ----------------------------------------------------------------------- intake


def intake(conn: sqlite3.Connection, run_id: str, text: str, language: str) -> dict[str, Any]:
    parsed = nlu.extract(text)
    parsed["language"] = language

    if parsed["injection"]:
        spans = nlu.injection_spans(text)
        db.add_evidence(
            conn, run_id,
            source_id="INPUT-1 injection",
            source_url="",
            extracted_value="Instruction-like text ignored and logged as data: " + "; ".join(spans)[:300],
            source="user",
            kind="prompt_injection",
        )
        db.add_decision(
            conn, run_id, "intake", "injection_ignored", 0.95,
            "Instruction-like text inside the citizen's request was treated as data, excluded from the draft, "
            "and cannot alter the approval gate.",
            "prompt-injection stance", {"spans": spans},
        )

    info = parsed["information_needed"]
    meaningful = len(info) >= 12 and (parsed["records_signal"] or parsed["action_signal"])
    state = parsed["state"]
    department = parsed["department"]

    if info:
        db.add_field(conn, run_id, "information_needed", info, 0.9 if meaningful else 0.4, "user")
    if state:
        db.add_field(conn, run_id, "state", state, 0.92, "user")
    if department:
        dept_conf = _fmt_confidence(0.55 + min(0.4, parsed["department_score"] / 10))
        db.add_field(conn, run_id, "department", department, dept_conf, "user")
    db.add_field(conn, run_id, "bpl", "true" if parsed["bpl"] else "false", 0.95, "user")
    db.add_field(conn, run_id, "applicant_name", parsed["applicant_name"], 0.9, "user")

    missing = []
    if not meaningful:
        missing.append("information_needed")
    if not state:
        missing.append("state")
    if not department:
        missing.append("department")

    db.add_decision(
        conn, run_id, "intake", "fields_extracted", 0.88 if not missing else 0.6,
        "Extracted " + ", ".join(sorted(k for k in ("state", "department", "bpl") if parsed.get(k)))
        + f" from {language} input; missing: {', '.join(missing) or 'none'}.",
        "intake", {
            "missing": missing,
            "records_signal": parsed["records_signal"],
            "action_signal": parsed["action_signal"],
            "department_score": parsed["department_score"],
        },
    )
    db.add_evidence(
        conn, run_id,
        source_id="INPUT-1 extraction",
        source_url="",
        extracted_value=(
            f"state={state or 'missing'}; department={department or 'missing'}; "
            f"bpl={parsed['bpl']}; records={parsed['records_signal']}; action={parsed['action_signal']}"
        ),
        source="user",
        kind="extraction",
    )
    return {"parsed": parsed, "missing": missing}


# ----------------------------------------------------------------------- triage


def triage(conn: sqlite3.Connection, run_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    personal = parsed["personal_data"]
    action = parsed["action_signal"]
    records = parsed["records_signal"]

    if personal:
        label, confidence = "exempt", 0.92
        reason = (
            "The request names a third party and asks for personal details (address/phone). "
            "Section 8(1)(j) applies: personal information unrelated to public activity is exempt "
            "unless the PIO finds a larger public interest. Filing is blocked pending human review."
        )
        rule_ref = "8(1)(j)"
    elif action and records:
        label, confidence = "mixed", 0.82
        reason = (
            "The request contains both an action demand (a repair or complaint) and a records question. "
            "Mixed requests are split: the action part goes to the grievance portal, the records part "
            "continues as an RTI application."
        )
        rule_ref = "6(1) + grievance routing"
    elif action:
        label, confidence = "grievance", 0.88
        reason = (
            "The citizen wants something done (fix, remove, act), not records that already exist. "
            "An RTI application cannot order action, so nothing is filed here; the request is redirected."
        )
        rule_ref = "RTI scope: records, not action"
    elif records:
        label, confidence = "rti", 0.86
        reason = (
            "The request asks for existing records or figures, which is the proper subject of an RTI "
            "application under Section 6(1)."
        )
        rule_ref = "6(1)"
    else:
        label, confidence = "uncertain", 0.3
        reason = "No clear signal whether the citizen wants records or action. Asking before proceeding."
        rule_ref = "triage confidence threshold"

    decision_id = db.add_decision(conn, run_id, "triage", label, confidence, reason, rule_ref, {
        "action_signal": action, "records_signal": records, "personal_data": personal,
    })

    if label == "exempt":
        db.add_evidence(
            conn, run_id, source_id="8(1)(j)", source_url="",
            extracted_value="Personal data of a third party identified in the request; filing blocked.",
            source="rules", kind="exemption",
        )
        db.add_decision(
            conn, run_id, "exemption_screen", "blocked", 0.93,
            "Section 8(1)(j) screen fired before any drafting; the run stops at the review queue.",
            "8(1)(j)", {"section": "8(1)(j)"},
        )
    if label == "mixed":
        db.add_decision(
            conn, run_id, "triage_split", "grievance", 0.84,
            "Split-out action part of the mixed request: the repair demand is routed to the grievance "
            "portal, not filed as RTI.",
            "grievance routing", {"split_from": decision_id, "grievance_url": GRIEVANCE_URL},
        )
        db.add_evidence(
            conn, run_id, source_id="RULE-mixed", source_url=GRIEVANCE_URL,
            extracted_value="Mixed request split into a grievance redirect and an RTI records request.",
            source="rules", kind="split",
        )

    suggestion = llm.review_triage(parsed.get("clean_text", ""), label)
    if suggestion:
        db.add_decision(
            conn, run_id, "llm_review", "advisory", 0.5,
            "Claude review recorded as advisory only; the deterministic label stands and the gate is unaffected.",
            "advisory", suggestion,
        )

    return {"label": label, "confidence": confidence, "reason": reason, "rule_ref": rule_ref,
            "decision_id": decision_id}


# ---------------------------------------------------------------------- urgency


def urgency(conn: sqlite3.Connection, run_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    urgent = bool(parsed["urgency"])
    if urgent:
        label = "life_liberty_48h"
        reason = (
            "The request contains an explicit life-or-liberty indicator, so the 48-hour clock under "
            "Section 7(1) applies instead of the standard 30 days."
        )
        confidence = 0.9
    else:
        label = "standard_30d"
        reason = "No explicit life-or-liberty indicator; the standard 30-day response period applies."
        confidence = 0.84
    db.add_field(conn, run_id, "urgency", "urgent" if urgent else "standard", confidence, "inferred")
    db.add_decision(conn, run_id, "urgency", label, confidence, reason, "7(1)", {"urgent": urgent})
    return {"urgent": urgent, "label": label, "rule_ref": "7(1)"}


# ---------------------------------------------------------------------- routing


def routing(conn: sqlite3.Connection, run_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    state = parsed["state"]
    department = parsed["department"]
    level = parsed["level"]

    if not state or not department:
        missing = "state" if not state else "department"
        db.add_decision(
            conn, run_id, "routing", "UNCERTAIN", 0.25,
            f"Cannot route without {missing}; no authority lookup was attempted.",
            "6(1)", {"missing": missing},
        )
        return {"uncertain": True, "missing": missing, "authority": None, "confidence": 0.25}

    confidence = 0.5
    confidence += 0.25 if state else 0
    confidence += 0.1 if parsed["department_score"] > 0 else 0
    confidence += min(0.12, parsed["department_score"] / 40)
    confidence = _fmt_confidence(confidence)

    authority = registry.find(level, state, department)
    fallback_used = False
    if authority is None:
        authority = registry.fallback(state)
        fallback_used = True
        confidence = min(confidence, 0.55)

    if authority is None:
        db.add_decision(
            conn, run_id, "routing", "UNCERTAIN", 0.2,
            f"No registry entry for {state} / {department}; asking the citizen instead of guessing.",
            "6(1)", {"missing": "authority"},
        )
        return {"uncertain": True, "missing": "department", "authority": None, "confidence": 0.2}

    db.add_evidence(
        conn, run_id,
        source_id=authority["source_id"], source_url=authority["source_url"],
        retrieved_at=authority["retrieved_at"],
        extracted_value=f"{authority['name']} — {authority['pio']}",
        source="registry", kind="authority_lookup",
    )

    threshold = rules.routing_threshold()
    if confidence < threshold:
        db.add_decision(
            conn, run_id, "routing", "UNCERTAIN", confidence,
            f"Best match is {authority['name']} but confidence {confidence:.2f} is below the "
            f"{threshold:.2f} threshold, so the citizen is asked before any filing.",
            "6(1)", {"authority": authority["code"], "missing": "confirm_authority"},
        )
        return {"uncertain": True, "missing": "confirm_authority", "authority": authority, "confidence": confidence}

    reason = (
        f"Decided {'central' if level == 'central' else 'state'} first, then department "
        f"'{DEPARTMENT_LABELS.get(department, department)}', then the PIO. "
        + ("Registry had no exact department entry, so General Administration was used. " if fallback_used else "")
        + "If the PIO does not hold the records, Section 6(3) requires transfer within five days."
    )
    db.add_decision(conn, run_id, "routing", "routed", confidence, reason, "6(1) + 6(3)", {
        "authority": authority["code"],
        "authority_name": authority["name"],
        "pio": authority["pio"],
        "source_id": authority["source_id"],
        "level": level,
        "transfer_fallback": (
            "If another public authority holds these records, transfer the application within five days "
            "under Section 6(3) and inform the applicant."
        ),
    })
    return {"uncertain": False, "authority": authority, "confidence": confidence, "rule_ref": "6(1) + 6(3)"}


# --------------------------------------------------------------------- drafting


def _period(text: str) -> str:
    lowered = text.lower()
    m = re.search(r"\b(20\d\d)\s*[-–/]\s*(\d\d)\b", lowered)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"\b(20\d\d)\b", lowered)
    if m:
        return m.group(1)
    if any(k in lowered for k in ("last year", "last financial year", "पिछले साल", "గత సంవత్సరం")):
        return "the last financial year"
    return "the most recent completed financial year"


def drafting(
    conn: sqlite3.Connection, run_id: str, parsed: dict[str, Any], route: dict[str, Any],
    triage_result: dict[str, Any],
) -> dict[str, Any]:
    authority = route.get("authority") or {}
    department = parsed["department"] or "general_administration"
    state = parsed["state"] or ""
    text = parsed["clean_text"]
    lowered = text.lower()

    spending = any(k in lowered for k in (
        "spent", "spend", "spending", "expenditure", "amount", "cost", "budget",
        "खर्च", "ఖర్చు", "బడ్జెట్",
    ))
    contract = any(k in lowered for k in ("contract", "agreement", "tender", "work order", "ऒప్పందం", "अनुबंध", "నిబంధన"))
    opinion = parsed["opinion"]

    items: list[str] = [
        "Provide copies of all records, file notings and correspondence held by your office concerning "
        "the matter described below.",
    ]
    if spending:
        items.append(
            "Provide copies of the work orders, measurement books, bills and vouchers showing the amount "
            "spent, the dates of payment and the designation of the approving officer."
        )
    if contract:
        items.append(
            "Provide copies of the contract agreement, tender documents and the comparative statement of "
            "tenders for the work referred to above."
        )
    if opinion:
        items.append(
            "Provide copies of the file notings and the written reasons recorded for the decision referred "
            "to above."
        )
    if len(items) == 1:
        items.append(
            "Provide copies of the register or report in which the requested particulars are recorded, "
            "with the relevant dates."
        )

    body_lines = [f"{i}. {item}" for i, item in enumerate(items, start=1)]
    body_lines += [
        "",
        f"Matter: {text.strip()}",
        f"Location: {state}." if state else "Location: not specified.",
        f"Period: {_period(text)}.",
        "",
        "Please supply photocopies or certified copies. If any part is held by another public authority, "
        "transfer that part under Section 6(3) within five days and inform me.",
    ]
    body = "\n".join(body_lines)

    notes: list[str] = [
        "Converted the citizen's narrative into numbered requests for records that already exist.",
        "Each item asks for copies of documents, not for opinions or explanations.",
    ]
    if spending:
        notes.append(
            "Added work orders, measurement books, bills and vouchers so the expenditure can be identified "
            "and checked."
        )
    if contract:
        notes.append("Added the contract, tender documents and comparative statement.")
    if opinion:
        notes.append(
            "Replaced the opinion-seeking 'why' question with a request for the reasons recorded on file; "
            "RTI covers records, not explanations."
        )
    if len(items) > 2:
        notes.append(f"Split the request into {len(items)} specific parts so each can be answered separately.")
    notes.append("Added location and period so the PIO can locate the correct file.")
    notes.append("No third party's personal data is requested.")

    precedent_note = ""
    precedents = registry.precedents_for(department, text)
    if precedents:
        p = precedents[0]
        db.add_evidence(
            conn, run_id, source_id=p["precedent_id"], source_url="",
            extracted_value=f"Similar past RTI: {p['title']} — {p['outcome']}",
            source="precedents", kind="precedent",
        )
        notes.append(f"Wording informed by a similar past request ({p['precedent_id']}): {p['outcome']}")
        precedent_note = p["precedent_id"]

    if parsed["injection"]:
        notes.append(
            "Removed instruction-like text found in the request; it was logged as evidence and had no effect."
        )

    subject = f"Records request — {DEPARTMENT_LABELS.get(department, department)}, {state}".strip(", ")
    draft_id = db.add_draft(
        conn, run_id, kind="application", subject=subject, text=body, rewrite_notes=notes,
    )
    db.add_decision(
        conn, run_id, "drafting", "draft_ready", 0.86,
        f"Drafted {len(items)} numbered record requests with {len(notes)} rewrite notes"
        + (f"; precedent {precedent_note} retrieved." if precedent_note else "."),
        "6(1)", {"draft_id": draft_id, "items": len(items), "precedent": precedent_note},
    )
    db.add_evidence(
        conn, run_id, source_id=draft_id, source_url="",
        extracted_value=f"Application draft v1 for {authority.get('code', 'unrouted')} with {len(notes)} notes",
        source="agent", kind="draft",
    )
    return {"draft_id": draft_id, "subject": subject, "text": body, "rewrite_notes": notes}


# -------------------------------------------------------------------------- fee


def fee(conn: sqlite3.Connection, run_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    default_fee = rules.fee_default()
    bpl = bool(parsed["bpl"])
    exempt = bpl and rules.bpl_exempt()
    amount = 0 if exempt else default_fee
    if exempt:
        label = "nil_bpl"
        reason = (
            "Applicant declared BPL, so the seeded rules waive the fee. The BPL declaration is a seeded "
            "assumption; the portal may ask for proof."
        )
    else:
        label = "standard_fee"
        reason = (
            f"Rs {default_fee} is the seeded default application fee. State variations are marked as seeded "
            "assumptions, not verified law."
        )
    db.add_field(conn, run_id, "fee_inr", str(amount), 0.95, "rules")
    db.add_decision(conn, run_id, "fee", label, 0.92, reason, "fee", {
        "fee_inr": amount, "bpl": bpl, "seeded_assumption": True,
    })
    return {"fee_inr": amount, "label": label, "bpl": bpl}
