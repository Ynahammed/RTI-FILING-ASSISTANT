"""Fee provenance is reported, not promised in a disclaimer.

The current data is unverified on purpose. These tests hold the line that the
unverified state is *recorded* (reason, dated source, correct flag) and that any
claim of verification has to point at a verified statutory section.
"""

from __future__ import annotations

import copy
import json

from scripts import check_fees


def current() -> tuple[dict, dict]:
    return check_fees.load()


def test_report_shape_and_current_coverage():
    rules, registry = current()
    report = check_fees.audit(rules, registry)

    assert report["problems"] == []
    coverage = report["coverage"]
    assert coverage["jurisdictions"] == 36
    assert coverage["state_entries"] == 4
    assert set(coverage["with_fee_entry"]) == {"Maharashtra", "Telangana", "Karnataka"}
    assert len(coverage["missing_fee_entry"]) == 33
    assert coverage["verified_fee_entries"] == []
    assert coverage["central_verified"] is True
    assert report["gate"]["structural_problems"] == 0
    assert report["entries"]["Telangana"]["tiered"] is True
    assert report["entries"]["Central"]["citation_section"] == "fee"


def test_current_data_exits_zero_without_strict(tmp_path):
    out = tmp_path / "fee_results.json"
    assert check_fees.main(["--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["strict"] is False
    assert payload["coverage"]["central_verified"] is True


def test_strict_mode_fails_today_and_says_so(tmp_path):
    out = tmp_path / "fee_results.json"
    assert check_fees.main(["--strict", "--out", str(out)]) == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["strict_failures"] == 36 - len(payload["coverage"]["verified_fee_entries"])


def test_a_missing_citation_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    del broken["state_assumptions"]["Maharashtra"]["citations"]
    report = check_fees.audit(broken, registry)
    assert any("Maharashtra: seeded fee cites no source" in p for p in report["problems"])


def test_a_non_https_source_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Karnataka"]["citations"][0]["url"] = "http://example.invalid/fee.pdf"
    report = check_fees.audit(broken, registry)
    assert any("Karnataka: citation URL is not an absolute https URL" in p for p in report["problems"])


def test_a_verified_claim_without_a_statutory_section_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Karnataka"]["verified"] = True
    report = check_fees.audit(broken, registry)
    assert any("Karnataka: claims verified without a citation_section" in p for p in report["problems"])


def test_a_verified_claim_pointing_at_an_unverified_section_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Central"]["citation_section"] = "19"
    for citation in broken["sections"]["19"]["citations"]:
        citation["verified"] = False
    report = check_fees.audit(broken, registry)
    assert any("Central: citation_section 19 has no verified citation" in p for p in report["problems"])


def test_a_tiered_schedule_without_a_citation_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Telangana"]["citations"] = []
    report = check_fees.audit(broken, registry)
    assert report["entries"]["Telangana"]["tiered"] is True
    assert any("Telangana: seeded fee cites no source" in p for p in report["problems"])


def test_an_invalid_tier_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Telangana"]["fee_schedule"] = [{"level": "village", "fee_inr": -1}]
    report = check_fees.audit(broken, registry)
    assert any("Telangana: fee_schedule[0] fee_inr is not a non-negative integer" in p for p in report["problems"])


def test_an_unverified_entry_without_a_reason_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Maharashtra"]["why_unverified"] = "   "
    report = check_fees.audit(broken, registry)
    assert any("Maharashtra: unverified but no reason recorded" in p for p in report["problems"])


def test_a_future_retrieval_date_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Karnataka"]["citations"][0]["retrieved_at"] = "2999-01-01"
    report = check_fees.audit(broken, registry)
    assert any("Karnataka: citation retrieved_at is in the future" in p for p in report["problems"])


def test_a_citation_cannot_claim_verification_inside_an_unverified_entry():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Maharashtra"]["citations"][0]["verified"] = True
    report = check_fees.audit(broken, registry)
    assert any(
        "Maharashtra: citation claims verification for an unverified entry" in p
        for p in report["problems"]
    )


def test_a_fee_entry_for_an_unknown_jurisdiction_is_rejected():
    rules, registry = current()
    broken = copy.deepcopy(rules)
    broken["state_assumptions"]["Atlantis"] = copy.deepcopy(broken["state_assumptions"]["Karnataka"])
    report = check_fees.audit(broken, registry)
    assert any("Atlantis: fee entry for an unknown jurisdiction" in p for p in report["problems"])
