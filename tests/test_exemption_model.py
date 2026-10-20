from __future__ import annotations

import json

import pytest

from agent import agents, db, exemption_model, llm


def _tiny_model(threshold: float = 0.5) -> dict:
    # "bank" pushes the logit up, "road" pushes it down, with unit idf and no intercept.
    return {
        "vocabulary": {"bank": 0, "road": 1, "bank road": 2},
        "idf": [1.0, 1.0, 1.0],
        "coef": [4.0, -4.0, 0.0],
        "intercept": 0.0,
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


def test_ngrams_lowercase_and_add_bigrams():
    grams = exemption_model.ngrams("Bank-Account, 42!")
    assert grams == ["bank", "account", "42", "bank account", "account 42"]


def test_ngrams_drop_single_character_tokens():
    assert exemption_model.ngrams("a b cd") == ["cd"]


def test_probability_is_higher_for_pattern_words():
    model = _tiny_model()
    assert exemption_model.probability(model, "bank") > 0.5
    assert exemption_model.probability(model, "road") < 0.5


def test_probability_is_neutral_for_out_of_vocabulary_text():
    assert exemption_model.probability(_tiny_model(), "zebra") == pytest.approx(0.5)


def test_probability_uses_sublinear_tf():
    model = _tiny_model()
    assert exemption_model.probability(model, "bank road") == pytest.approx(0.5, abs=1e-9)
    # Sublinear tf: weight(bank) = 1 + ln 3, weight(road) = 1, and the in-vocabulary bigram
    # "bank road" (zero coef) also enters the L2 norm. Raw tf would give about 0.918 here.
    assert exemption_model.probability(model, "bank bank bank road") == pytest.approx(0.8502, abs=1e-3)


def test_shipped_model_file_is_well_formed():
    path = exemption_model.MODEL_PATH
    if not path.exists():
        pytest.skip("shipped model not present")
    model = json.loads(path.read_text(encoding="utf-8"))
    assert len(model["idf"]) == len(model["coef"]) == len(model["vocabulary"])
    assert sorted(model["vocabulary"].values()) == list(range(len(model["vocabulary"])))
    assert 0.0 < model["threshold"] < 1.0
    score = exemption_model.probability(model, "How much was spent on the road repair contract?")
    assert 0.0 <= score <= 1.0


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
