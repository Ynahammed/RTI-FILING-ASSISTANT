from __future__ import annotations

import json
import math

import pytest

from agent import agents, db, exemption_model, llm


def _tiny_model(threshold: float = 0.5) -> dict:
    return {
        "pos_counts": {"bank": 5},
        "neg_counts": {"road": 5},
        "vocab_size": 2,
        "log_prior_pos": math.log(0.5),
        "log_prior_neg": math.log(0.5),
        "pos_total": 5,
        "neg_total": 5,
        "threshold": threshold,
    }


@pytest.fixture()
def model_file(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    path.write_text(json.dumps(_tiny_model()), encoding="utf-8")
    monkeypatch.setattr(exemption_model, "MODEL_PATH", path)
    monkeypatch.setenv("RTI_EXEMPTION_ADVISORY", "1")
    exemption_model._load.cache_clear()
    yield path
    exemption_model._load.cache_clear()


def test_tokenize_lowercases_and_splits_on_non_alphanumerics():
    assert exemption_model.tokenize("Bank-Account, 42!") == ["bank", "account", "42"]


def test_posterior_is_higher_for_pattern_words(model_file):
    model = _tiny_model()
    assert exemption_model.posterior(model, "bank") > 0.5
    assert exemption_model.posterior(model, "road") < 0.5


def test_advisory_flags_matching_text(model_file):
    flag = exemption_model.advisory_flag("bank")
    assert flag is not None
    assert flag["threshold"] == 0.5
    assert flag["score"] >= 0.5


def test_advisory_ignores_non_matching_text(model_file):
    assert exemption_model.advisory_flag("road") is None


def test_advisory_ignores_empty_text(model_file):
    assert exemption_model.advisory_flag("   ") is None


def test_advisory_is_off_by_default(model_file, monkeypatch):
    monkeypatch.delenv("RTI_EXEMPTION_ADVISORY", raising=False)
    assert exemption_model.advisory_flag("bank") is None


def test_advisory_can_be_disabled_by_env(model_file, monkeypatch):
    monkeypatch.setenv("RTI_EXEMPTION_ADVISORY", "0")
    assert exemption_model.advisory_flag("bank") is None


def test_advisory_is_none_when_model_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(exemption_model, "MODEL_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("RTI_EXEMPTION_ADVISORY", "1")
    exemption_model._load.cache_clear()
    try:
        assert exemption_model.advisory_flag("bank") is None
    finally:
        exemption_model._load.cache_clear()


def _decisions(conn, run_id):
    rows = conn.execute("SELECT agent, label FROM decisions WHERE run_id = ?", (run_id,)).fetchall()
    return [(row[0], row[1]) for row in rows]


def _parsed(**overrides):
    base = {
        "personal_data": False,
        "action_signal": False,
        "records_signal": True,
        "information_needed": "How much was spent on the bank loan scheme?",
        "clean_text": "How much was spent on the bank loan scheme?",
    }
    base.update(overrides)
    return base


def test_triage_records_advisory_without_changing_label(conn, monkeypatch):
    monkeypatch.setattr(exemption_model, "advisory_flag", lambda text: {"score": 0.9, "threshold": 0.4})
    monkeypatch.setattr(llm, "review_triage", lambda *a, **k: None)
    run_id = db.create_run(conn, language="en", raw_input="x")

    result = agents.triage(conn, run_id, _parsed())

    assert result["label"] == "rti"
    decisions = _decisions(conn, run_id)
    assert ("triage", "rti") in decisions
    assert ("exemption_advisory", "advisory") in decisions
    assert ("exemption_screen", "blocked") not in decisions


def test_triage_skips_advisory_when_no_flag(conn, monkeypatch):
    monkeypatch.setattr(exemption_model, "advisory_flag", lambda text: None)
    monkeypatch.setattr(llm, "review_triage", lambda *a, **k: None)
    run_id = db.create_run(conn, language="en", raw_input="x")

    agents.triage(conn, run_id, _parsed())

    assert all(agent != "exemption_advisory" for agent, _ in _decisions(conn, run_id))


def test_triage_does_not_add_advisory_when_already_exempt(conn, monkeypatch):
    monkeypatch.setattr(exemption_model, "advisory_flag", lambda text: {"score": 0.9, "threshold": 0.4})
    monkeypatch.setattr(llm, "review_triage", lambda *a, **k: None)
    run_id = db.create_run(conn, language="en", raw_input="x")

    result = agents.triage(conn, run_id, _parsed(personal_data=True, records_signal=False))

    assert result["label"] == "exempt"
    assert ("exemption_screen", "blocked") in _decisions(conn, run_id)
    assert all(agent != "exemption_advisory" for agent, _ in _decisions(conn, run_id))
