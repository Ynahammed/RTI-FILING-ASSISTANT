"""Coverage is real only if a generic entry cannot file itself."""

from __future__ import annotations

import json
from collections import Counter

from agent import agents, db, registry
from common import config

REQUIRED = (
    "code", "level", "state", "department", "name", "pio", "source_id", "source_url", "retrieved_at",
)


def data() -> dict:
    return json.loads(
        (config.PROJECT_ROOT / "rules" / "authority_registry.json").read_text(encoding="utf-8")
    )


def parsed(state: str, department: str) -> dict:
    return {
        "state": state,
        "department": department,
        "level": "central" if state == "Central" else "state",
        "department_score": 3,
    }


def test_every_state_and_union_territory_is_covered():
    payload = data()
    covered = {a["state"] for a in payload["authorities"]}
    assert set(payload["jurisdictions"]) <= covered
    assert len(payload["jurisdictions"]) == 36


def test_entries_carry_the_provenance_the_ledger_needs():
    for authority in data()["authorities"]:
        for field in REQUIRED:
            assert str(authority.get(field, "")).strip(), (authority.get("code"), field)
        assert authority["source_url"].startswith("https://")
        assert authority.get("generic") is not True or authority.get("verified") is False


def test_codes_are_unique():
    codes = Counter(a["code"] for a in data()["authorities"])
    assert [code for code, count in codes.items() if count > 1] == []


def test_a_generic_state_fallback_asks_before_filing(conn):
    run_id = db.create_run(conn, language="en", raw_input="Road records in Gujarat")
    result = agents.routing(conn, run_id, parsed("Gujarat", "public_works"))

    assert result["uncertain"] is True
    assert result["missing"] == "confirm_authority"
    assert result["authority"]["generic"] is True
    assert result["confidence"] <= 0.55
    decision = conn.execute(
        "SELECT * FROM decisions WHERE run_id=? AND agent='routing'", (run_id,)
    ).fetchone()
    assert decision["label"] == "UNCERTAIN"
    assert "confirm" in json.loads(decision["output"] or "{}").get("missing", "")


def test_a_verified_state_still_routes_at_high_confidence(conn):
    run_id = db.create_run(conn, language="en", raw_input="Road records in Pune")
    result = agents.routing(conn, run_id, parsed("Maharashtra", "public_works"))

    assert result["uncertain"] is False
    assert result["authority"]["code"] == "MH-PWD-01"
    assert result["confidence"] >= 0.7


def test_a_generic_entry_is_not_presented_as_a_verified_office():
    generic = [a for a in registry.authorities() if a.get("generic")]
    assert generic
    for authority in generic:
        assert "generic" in authority["name"].lower()
        assert authority["verified"] is False


def test_coverage_artifact_gate_has_no_gaps():
    artifact = config.PROJECT_ROOT / "artifacts" / "registry_coverage_results.json"
    if not artifact.exists():
        return
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["gate"]["uncovered"] == 0
    assert payload["gate"]["missing_provenance"] == 0
    assert payload["gate"]["duplicate_codes"] == 0
