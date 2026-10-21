"""Review routing: a classifier flag queues a request, it does not reject it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import agents, db, exemption_model, rules, supervisor
from common import config

ARTIFACT = Path("artifacts/review_routing_results.json")


@pytest.fixture()
def parsed():
    return {
        "personal_data": False,
        "action_signal": False,
        "records_signal": True,
        "information_needed": "Copies of the contract and payment records for the road work.",
        "clean_text": "Copies of the contract and payment records for the road work.",
    }


def test_high_score_goes_to_review(conn, parsed, monkeypatch):
    monkeypatch.setattr(exemption_model, "score", lambda text: 0.9)
    run_id = db.create_run(conn, language="en", raw_input="Records request")
    result = agents.triage(conn, run_id, parsed)

    assert result["label"] == "rti"
    assert result["review_required"] == {"score": 0.9, "threshold": rules.review_threshold()}
    decision = conn.execute(
        "SELECT * FROM decisions WHERE run_id=? AND agent='exemption_screen'", (run_id,)
    ).fetchone()
    assert decision["label"] == "needs_review"
    assert decision["rule_ref"] == "8(1)(j) review"


def test_low_score_files_normally(conn, parsed, monkeypatch):
    monkeypatch.setattr(exemption_model, "score", lambda text: 0.1)
    run_id = db.create_run(conn, language="en", raw_input="Records request")
    result = agents.triage(conn, run_id, parsed)
    assert result["label"] == "rti"
    assert result["review_required"] is None


def test_threshold_comes_from_measured_rules(conn):
    data = json.loads((config.PROJECT_ROOT / "rules" / "rti_rules.json").read_text(encoding="utf-8"))
    block = data["exemption_review"]
    assert rules.review_threshold() == block["threshold"]
    assert block["measured"]["recall"] > block["measured"]["rule_only_recall"]
    assert block["artifact"] == "artifacts/review_routing_results.json"


@pytest.mark.skipif(not ARTIFACT.exists(), reason="routing benchmark has not been run")
def test_measured_routing_meets_its_budget():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    gate = data["gate"]
    assert gate["recall"] >= gate["min_recall"]
    assert gate["review_load"] <= gate["max_review_load"]
    assert gate["nested_recall"] >= gate["min_recall"] - 0.02
    # The honest re-selection must choose essentially the shipped threshold. One fold
    # lands at 0.50 rather than 0.42 because its inner rows miss the precision floor by
    # 0.008 at 0.42; the pooled nested recall above is the number that has to hold, and
    # the bound here only catches a collapse (a fold choosing a threshold twice as high).
    spread = max(data["nested"]["threshold_per_outer_fold"]) - min(
        data["nested"]["threshold_per_outer_fold"]
    )
    assert spread <= 0.10, spread


def test_deterministic_rule_still_blocks_before_review(conn, parsed, monkeypatch):
    monkeypatch.setattr(exemption_model, "score", lambda text: 0.99)
    parsed["personal_data"] = True
    run_id = db.create_run(conn, language="en", raw_input="Personal data request")
    result = agents.triage(conn, run_id, parsed)
    assert result["label"] == "exempt"
    assert result["review_required"] is None


def test_routed_run_lands_in_the_review_queue(conn, monkeypatch):
    monkeypatch.setattr(exemption_model, "score", lambda text: 0.9)
    run_id, _ = supervisor.start_run(
        "How much did the Public Works Department spend repairing the road in Ward 12, "
        "Pune, during 2025-26?"
    )
    detail = supervisor.detail(run_id)
    assert detail["run"]["status"] == "review_queue"
    assert any("human reviewer" in m["text"] for m in detail["messages"])
    assert detail["approval"] is None
    assert detail["actions"] == []


def test_grievance_is_not_queued_for_review(conn, monkeypatch):
    monkeypatch.setattr(exemption_model, "score", lambda text: 0.9)
    run_id, _ = supervisor.start_run(
        "The street light near my house in Pune has been broken for three weeks. "
        "Please fix it immediately."
    )
    detail = supervisor.detail(run_id)
    assert detail["run"]["status"] == "redirected"
