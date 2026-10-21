"""Hand-computed checks for common/metrics.py.

Each expected value is derived by hand in the docstring so the test documents the
definition rather than restating the implementation.
"""

from __future__ import annotations

import pytest

from common import metrics


def test_prf() -> None:
    # tp=3, fp=1, fn=2 -> precision 0.75, recall 0.6, f1 0.6667
    assert metrics.prf(3, 1, 2) == {
        "tp": 3,
        "fp": 1,
        "fn": 2,
        "precision": 0.75,
        "recall": 0.6,
        "f1": 0.6667,
    }


def test_prf_with_no_predictions() -> None:
    assert metrics.prf(0, 0, 5)["f1"] == 0.0


def test_micro_macro_f1() -> None:
    # rows: pred [{a},{b}] vs truth [{a,b},{a}]
    #   a: tp=1 fp=0 fn=1 -> f1 0.6667
    #   b: tp=0 fp=1 fn=1 -> f1 0.0
    # micro: tp=1 fp=1 fn=2 -> p=0.5 r=0.3333 f1=0.4
    result = metrics.micro_macro_f1([{"a"}, {"b"}], [{"a", "b"}, {"a"}], ["a", "b"])
    assert result["micro_f1"] == 0.4
    assert result["per_label"]["a"]["f1"] == 0.6667
    assert result["per_label"]["b"]["f1"] == 0.0
    assert result["macro_f1"] == pytest.approx(0.3333, abs=1e-4)


def test_hamming_loss_and_subset_accuracy() -> None:
    # row 0 differs in {b}; row 1 differs in {a,b}: 3 wrong of 4 decisions.
    assert metrics.hamming_loss([{"a"}, {"b"}], [{"a", "b"}, {"a"}], ["a", "b"]) == 0.75
    assert metrics.subset_accuracy([{"a"}, {"a", "b"}], [{"a"}, {"a"}]) == 0.5


def test_average_precision() -> None:
    # ranks 1 and 3 are positive: (1 + 2/3) / 2 = 0.8333
    assert metrics.average_precision([1, 0, 1], [0.9, 0.8, 0.7]) == 0.8333


def test_average_precision_with_no_positives() -> None:
    assert metrics.average_precision([0, 0], [0.9, 0.1]) == 0.0


def test_roc_auc_perfect_and_tied() -> None:
    assert metrics.roc_auc([1, 1, 0], [0.9, 0.8, 0.7]) == 1.0
    # The negative sits above the second positive, so only one of two pairs is ordered.
    assert metrics.roc_auc([1, 0, 1], [0.9, 0.8, 0.7]) == 0.5
    # Both scores tied: the positive takes the average rank 1.5 -> AUC 0.5
    assert metrics.roc_auc([1, 0], [0.5, 0.5]) == 0.5
    # Inverted ranking -> 0.0
    assert metrics.roc_auc([0, 1], [0.9, 0.1]) == 0.0


def test_ranking_metrics() -> None:
    ranked = ["a", "b", "c"]
    relevant = {"b", "c"}
    # first 2 of 3 ranks hold one relevant item
    assert metrics.recall_at_k(ranked, relevant, 2) == 0.5
    assert metrics.precision_at_k(ranked, relevant, 2) == 0.5
    # first relevant at rank 2
    assert metrics.mrr(ranked, relevant) == 0.5
    # DCG = 1/log2(3); IDCG = 1 + 1/log2(3); ratio = 0.3869
    assert metrics.ndcg_at_k(ranked, relevant, 2) == 0.3869


def test_ranking_metrics_without_relevant_hits() -> None:
    ranked = ["x", "y"]
    relevant = {"z"}
    assert metrics.recall_at_k(ranked, relevant, 2) == 0.0
    assert metrics.mrr(ranked, relevant) == 0.0
    assert metrics.ndcg_at_k(ranked, relevant, 2) == 0.0


def test_brier() -> None:
    # ((1-0.8)^2 + (0-0.2)^2) / 2 = 0.04
    assert metrics.brier([1, 0], [0.8, 0.2]) == 0.04


def test_expected_calibration_error() -> None:
    # bin 0.9: confidence .95, accuracy 1 -> error .05 weighted by 0.5 = .025
    # bin 0.0: confidence .05, accuracy 0 -> error .05 weighted by 0.5 = .025
    assert metrics.expected_calibration_error([1, 0], [0.95, 0.05], bins=10) == 0.05
    # A perfectly calibrated constant predictor at the base rate has zero ECE
    assert metrics.expected_calibration_error([1, 0, 1, 0], [0.5, 0.5, 0.5, 0.5]) == 0.0


def test_mean() -> None:
    assert metrics.mean([0.5, 1.0]) == 0.75
    assert metrics.mean([]) == 0.0
