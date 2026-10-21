"""Gate logic in scripts/bench_all.py.

The runner must fail closed: a missing artifact or a score below its floor is a
failure, never a silent pass.
"""

from __future__ import annotations

from scripts import bench_all


def test_dig_walks_nested_paths() -> None:
    data = {"gate": {"roc_auc": 0.71}, "nested": {"list": [1, 2]}}
    assert bench_all.dig(data, ("gate", "roc_auc")) == 0.71
    assert bench_all.dig(data, ("nested", "list")) == [1, 2]
    assert bench_all.dig(data, ("gate", "missing")) is None
    assert bench_all.dig(data, ("absent", "x")) is None


def test_all_gates_pass_with_healthy_artifacts(monkeypatch) -> None:
    artifacts = {
        "classifier_8_1_j_results.json": {"gate": {"roc_auc": 0.717, "f1_lift": 0.166, "ece_after": 0.031}},
        "exemptions_multilabel_results.json": {
            "gate": {"ap_lift": 3.55},
            "nested_variant_selection": {"pooled_macro_ap": 0.2269},
            "calibration": {"ece_after": 0.0127},
        },
        "retrieval_results.json": {"gate": {"precision_lift": 2.11, "authority_precision_lift": 4.15}},
    }
    monkeypatch.setattr(bench_all, "read", lambda name: artifacts.get(name, {}))
    checks = bench_all.gates()
    assert len(checks) == 8
    assert all(item["passed"] for item in checks)


def test_a_low_score_fails_its_gate(monkeypatch) -> None:
    artifacts = {
        "classifier_8_1_j_results.json": {"gate": {"roc_auc": 0.717, "f1_lift": 0.166, "ece_after": 0.031}},
        "exemptions_multilabel_results.json": {
            "gate": {"ap_lift": 3.55},
            "nested_variant_selection": {"pooled_macro_ap": 0.2269},
            "calibration": {"ece_after": 0.0127},
        },
        "retrieval_results.json": {"gate": {"precision_lift": 1.10, "authority_precision_lift": 4.15}},
    }
    monkeypatch.setattr(bench_all, "read", lambda name: artifacts.get(name, {}))
    failing = [item["gate"] for item in bench_all.gates() if not item["passed"]]
    assert failing == ["retrieval P@10 beats chance 1.5x"]


def test_missing_artifacts_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(bench_all, "read", lambda name: {})
    checks = bench_all.gates()
    assert checks and all(not item["passed"] for item in checks)


def test_zero_lift_fails_the_strict_gate(monkeypatch) -> None:
    # f1_lift uses ">" not ">=", so zero improvement must fail.
    artifacts = {
        "classifier_8_1_j_results.json": {"gate": {"roc_auc": 0.717, "f1_lift": 0.0, "ece_after": 0.031}},
        "exemptions_multilabel_results.json": {
            "gate": {"ap_lift": 3.55},
            "nested_variant_selection": {"pooled_macro_ap": 0.2269},
            "calibration": {"ece_after": 0.0127},
        },
        "retrieval_results.json": {"gate": {"precision_lift": 2.11, "authority_precision_lift": 4.15}},
    }
    monkeypatch.setattr(bench_all, "read", lambda name: artifacts.get(name, {}))
    failing = [item["gate"] for item in bench_all.gates() if not item["passed"]]
    assert failing == ["8(1)(j) nested F1 beats keyword rule"]


def test_upper_bound_gate_fails_when_ece_is_too_high(monkeypatch) -> None:
    artifacts = {
        "classifier_8_1_j_results.json": {"gate": {"roc_auc": 0.717, "f1_lift": 0.166, "ece_after": 0.031}},
        "exemptions_multilabel_results.json": {
            "gate": {"ap_lift": 3.55},
            "nested_variant_selection": {"pooled_macro_ap": 0.2269},
            "calibration": {"ece_after": 0.09},
        },
        "retrieval_results.json": {"gate": {"precision_lift": 2.11, "authority_precision_lift": 4.15}},
    }
    monkeypatch.setattr(bench_all, "read", lambda name: artifacts.get(name, {}))
    failing = [item["gate"] for item in bench_all.gates() if not item["passed"]]
    assert failing == ["calibrated ECE below 0.03"]
