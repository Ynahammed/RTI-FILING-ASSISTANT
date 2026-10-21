"""Legal provenance is enforced, not asserted in a disclaimer."""

from __future__ import annotations

import copy
import json

from agent import rules
from common import config


def data() -> dict:
    return json.loads((config.PROJECT_ROOT / "rules" / "rti_rules.json").read_text(encoding="utf-8"))


def test_every_required_rule_is_cited_and_verified():
    assert rules.validate() == []
    provenance = rules.provenance()
    assert provenance["uncited"] == 0
    assert provenance["sections_cited"] == provenance["sections_required"] == 5
    assert provenance["sections_verified"] == 5


def test_each_citation_has_a_url_quote_and_date():
    for ref, section in data()["sections"].items():
        for citation in section["citations"]:
            assert citation["url"].startswith("https://")
            assert len(citation["quote"]) > 30
            assert citation["retrieved_at"] == "2026-10-21"
            assert citation["verified"] is True


def test_a_missing_citation_is_rejected():
    broken = copy.deepcopy(data())
    del broken["sections"]["7(1)"]["citations"]
    problems = rules.validate(broken)
    assert any("7(1): no citation" in problem for problem in problems)


def test_a_false_verification_claim_is_rejected():
    broken = copy.deepcopy(data())
    broken["sections"]["19"]["citations"][0]["verified"] = False
    problems = rules.validate(broken)
    assert any("19: citation is not marked verified" in problem for problem in problems)


def test_a_state_fee_that_claims_verification_needs_a_source():
    broken = copy.deepcopy(data())
    broken["state_assumptions"]["Maharashtra"]["verified"] = True
    broken["state_assumptions"]["Maharashtra"].pop("citation_section", None)
    problems = rules.validate(broken)
    assert any("Maharashtra: claims verified without a citation" in problem for problem in problems)


def test_a_seeded_state_fee_must_say_why():
    broken = copy.deepcopy(data())
    broken["state_assumptions"]["Karnataka"]["why_unverified"] = ""
    problems = rules.validate(broken)
    assert any("Karnataka: seeded but no reason recorded" in problem for problem in problems)


def test_unverified_state_fees_are_not_presented_as_law():
    for state, block in data()["state_assumptions"].items():
        if state == "Central":
            continue
        assert block["seeded"] is True
        assert block["verified"] is False
        assert block["why_unverified"]


def test_every_state_fee_names_a_source():
    for state, block in data()["state_assumptions"].items():
        if block.get("citation_section"):
            # The central entry points at the verified fee section instead.
            continue
        assert block.get("citations"), f"{state} reports a fee but cites nothing"
        for citation in block["citations"]:
            assert citation["url"].startswith("https://")
            assert len(citation["quote"]) > 30
            assert citation["retrieved_at"] == "2026-10-21"
            assert citation["verified"] is False  # reported, not gazette-checked


def test_telangana_fee_is_tiered_not_flat():
    schedule = data()["state_assumptions"]["Telangana"]["fee_schedule"]
    assert {(row["level"], row["fee_inr"]) for row in schedule} == {
        ("village", 0), ("mandal", 5), ("other", 10),
    }


def test_a_seeded_state_fee_without_a_source_is_rejected():
    broken = copy.deepcopy(data())
    broken["state_assumptions"]["Maharashtra"]["citations"] = []
    problems = rules.validate(broken)
    assert any("Maharashtra: seeded without a source" in problem for problem in problems)
