"""Multi-label Section 8(1) exemption benchmark on RTI-Bench (Source A).

Where scripts/train_8_1_j_classifier.py scores one label (8(1)(j)), this scores every
exemption section with enough support to evaluate: 8(1)(j), 8(1)(d), 8(1)(h), 8(1)(e)
and 8(1)(g) at a 25-positive floor. 65 of the 1,084 scored rows cite more than one
section, so this is a genuine multi-label problem, not a relabelling.

Leakage: only `title` and `information_sought` are used. `instruction` and `response`
quote the PIO reply and the Commission's decision and name the section outright, so a
model trained on them learns the verdict. Every feature variant here is request text.

Evaluation:
* 5-fold CV, folds by hf_id % 5, pooled out-of-fold predictions.
* Threshold-free metrics (AP, ROC AUC) are the headline: they do not depend on a cut.
* F1@0.5 is reported without tuning.
* A nested pass picks one global threshold on inner folds only, so the honest F1 is
  not inflated by choosing the cut on the rows being scored.
* Calibration (Brier, ECE) is reported on the out-of-fold probabilities.

Requires scikit-learn (requirements-dev.txt).
Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.bench_exemptions
"""

from __future__ import annotations

import ast
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier

from common import metrics

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT = Path("artifacts/exemptions_multilabel_results.json")
TEXT_FIELDS = ("title", "information_sought")
MIN_SUPPORT = 25
FOLDS = 5
C = 4.0
THRESHOLDS = [round(t / 100, 2) for t in range(5, 96)]
VARIANTS = ("word", "char", "union")


def load() -> list[dict[str, Any]]:
    with DATA.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["outcome"] != "UNKNOWN"]
    for row in rows:
        try:
            cited = ast.literal_eval(row.get("exemptions_cited") or "[]")
        except (ValueError, SyntaxError):
            cited = []
        row["sections"] = set(cited if isinstance(cited, list) else [])
        row["fold"] = int(row["hf_id"]) % FOLDS
    return rows


def text_of(row: dict[str, Any]) -> str:
    return " ".join((row.get(f) or "").strip() for f in TEXT_FIELDS).strip()


def vectorize(variant: str, train_texts: list[str], test_texts: list[str]):
    """Fit the requested feature space on train only and transform both sides."""
    parts_train, parts_test = [], []
    if variant in ("word", "union"):
        word = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
        parts_train.append(word.fit_transform(train_texts))
        parts_test.append(word.transform(test_texts))
    if variant in ("char", "union"):
        char = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 6), sublinear_tf=True, min_df=1)
        parts_train.append(char.fit_transform(train_texts))
        parts_test.append(char.transform(test_texts))
    if len(parts_train) == 1:
        return parts_train[0], parts_test[0]
    return hstack(parts_train).tocsr(), hstack(parts_test).tocsr()


def fit_predict(variant: str, train: list[dict[str, Any]], test: list[dict[str, Any]], labels: list[str]) -> np.ndarray:
    """Out-of-sample probabilities, shape (len(test), len(labels))."""
    x_train, x_test = vectorize(variant, [text_of(r) for r in train], [text_of(r) for r in test])
    y_train = np.array([[1 if label in r["sections"] else 0 for label in labels] for r in train])
    clf = OneVsRestClassifier(
        LogisticRegression(C=C, class_weight="balanced", max_iter=2000, solver="liblinear")
    )
    clf.fit(x_train, y_train)
    return clf.predict_proba(x_test)


def oof(variant: str, rows: list[dict[str, Any]], labels: list[str]) -> np.ndarray:
    scores = np.zeros((len(rows), len(labels)))
    for k in range(FOLDS):
        test_idx = [i for i, r in enumerate(rows) if r["fold"] == k]
        train = [r for r in rows if r["fold"] != k]
        if not test_idx:
            continue
        scores[test_idx] = fit_predict(variant, train, [rows[i] for i in test_idx], labels)
    return scores


def predictions(scores: np.ndarray, labels: list[str], threshold: float) -> list[set[str]]:
    return [
        {label for j, label in enumerate(labels) if row[j] >= threshold}
        for row in scores
    ]


def evaluate_variant(
    name: str, scores: np.ndarray, truth: list[set[str]], labels: list[str]
) -> dict[str, Any]:
    per_label: dict[str, dict[str, float]] = {}
    for j, label in enumerate(labels):
        y = [1 if label in t else 0 for t in truth]
        s = [float(v) for v in scores[:, j]]
        per_label[label] = {
            "support": int(sum(y)),
            "ap": metrics.average_precision(y, s),
            "roc_auc": metrics.roc_auc(y, s),
            "brier": metrics.brier(y, s),
            "ece": metrics.expected_calibration_error(y, s),
        }
    at_half = metrics.micro_macro_f1(predictions(scores, labels, 0.5), truth, labels)
    return {
        "variant": name,
        "macro_ap": metrics.mean([v["ap"] for v in per_label.values()]),
        "macro_roc_auc": metrics.mean([v["roc_auc"] for v in per_label.values()]),
        "mean_brier": metrics.mean([v["brier"] for v in per_label.values()]),
        "mean_ece": metrics.mean([v["ece"] for v in per_label.values()]),
        "at_0.5": {
            "micro_f1": at_half["micro_f1"],
            "macro_f1": at_half["macro_f1"],
            "hamming_loss": metrics.hamming_loss(predictions(scores, labels, 0.5), truth, labels),
            "subset_accuracy": metrics.subset_accuracy(predictions(scores, labels, 0.5), truth),
        },
        "per_label": per_label,
    }


def nested_threshold(rows: list[dict[str, Any]], labels: list[str], variant: str) -> dict[str, Any]:
    """Pick one global cut on inner folds only; score the held-out outer fold."""
    pooled_pred: list[set[str]] = []
    pooled_truth: list[set[str]] = []
    chosen: list[float] = []
    for k in range(FOLDS):
        outer_train = [r for r in rows if r["fold"] != k]
        outer_test = [r for r in rows if r["fold"] == k]
        inner_scores = oof(variant, outer_train, labels)
        inner_truth = [r["sections"] for r in outer_train]
        best = max(
            THRESHOLDS,
            key=lambda t: metrics.micro_macro_f1(predictions(inner_scores, labels, t), inner_truth, labels)["micro_f1"],
        )
        chosen.append(best)
        test_scores = fit_predict(variant, outer_train, outer_test, labels)
        pooled_pred.extend(predictions(test_scores, labels, best))
        pooled_truth.extend(r["sections"] for r in outer_test)
    pooled = metrics.micro_macro_f1(pooled_pred, pooled_truth, labels)
    return {
        "threshold_per_outer_fold": chosen,
        "pooled_micro_f1": pooled["micro_f1"],
        "pooled_macro_f1": pooled["macro_f1"],
        "pooled_precision": pooled["micro_precision"],
        "pooled_recall": pooled["micro_recall"],
    }


def macro_ap(scores: np.ndarray, truth: list[set[str]], labels: list[str]) -> float:
    return metrics.mean(
        [
            metrics.average_precision(
                [1 if label in t else 0 for t in truth], [float(v) for v in scores[:, j]]
            )
            for j, label in enumerate(labels)
        ]
    )


def nested_variant_selection(rows: list[dict[str, Any]], labels: list[str]) -> dict[str, Any]:
    """Choose the feature variant inside each outer fold, then score the held-out fold.

    Comparing variants on the same rows that are reported is a selection bias. This is
    the honest version: the choice is made on inner folds only.
    """
    pooled_pred: list[set[str]] = []
    pooled_truth: list[set[str]] = []
    chosen: list[str] = []
    outer_scores = np.zeros((len(rows), len(labels)))
    for k in range(FOLDS):
        outer_train = [r for r in rows if r["fold"] != k]
        outer_test = [r for r in rows if r["fold"] == k]
        inner_truth = [r["sections"] for r in outer_train]
        inner = {variant: oof(variant, outer_train, labels) for variant in VARIANTS}
        best = max(VARIANTS, key=lambda v: macro_ap(inner[v], inner_truth, labels))
        chosen.append(best)
        test_scores = fit_predict(best, outer_train, outer_test, labels)
        pooled_pred.extend(predictions(test_scores, labels, 0.5))
        pooled_truth.extend(r["sections"] for r in outer_test)
        idx = [i for i, r in enumerate(rows) if r["fold"] == k]
        outer_scores[idx] = test_scores
    pooled = metrics.micro_macro_f1(pooled_pred, pooled_truth, labels)
    return {
        "variant_per_outer_fold": chosen,
        "pooled_macro_ap": macro_ap(outer_scores, [r["sections"] for r in rows], labels),
        "pooled_micro_f1_at_0.5": pooled["micro_f1"],
        "pooled_macro_f1_at_0.5": pooled["macro_f1"],
    }


def cross_fitted_calibration(
    rows: list[dict[str, Any]], scores: np.ndarray, labels: list[str]
) -> dict[str, Any]:
    """Isotonic calibration fitted on other folds only, so held-out rows stay unseen."""
    calibrated = np.zeros_like(scores)
    for k in range(FOLDS):
        test_idx = [i for i, r in enumerate(rows) if r["fold"] == k]
        train_idx = [i for i, r in enumerate(rows) if r["fold"] != k]
        if not test_idx or not train_idx:
            continue
        for j, label in enumerate(labels):
            y_train = [1 if label in rows[i]["sections"] else 0 for i in train_idx]
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            iso.fit(scores[train_idx, j], y_train)
            calibrated[test_idx, j] = iso.predict(scores[test_idx, j])
    before, after = [], []
    for j, label in enumerate(labels):
        y = [1 if label in t else 0 for t in (r["sections"] for r in rows)]
        before.append(metrics.expected_calibration_error(y, [float(v) for v in scores[:, j]]))
        after.append(metrics.expected_calibration_error(y, [float(v) for v in calibrated[:, j]]))
    return {
        "method": "isotonic, fitted on the other four folds",
        "ece_before": metrics.mean(before),
        "ece_after": metrics.mean(after),
        "brier_before": metrics.mean(
            [
                metrics.brier([1 if label in t else 0 for t in (r["sections"] for r in rows)], [float(v) for v in scores[:, j]])
                for j, label in enumerate(labels)
            ]
        ),
        "brier_after": metrics.mean(
            [
                metrics.brier([1 if label in t else 0 for t in (r["sections"] for r in rows)], [float(v) for v in calibrated[:, j]])
                for j, label in enumerate(labels)
            ]
        ),
    }


def baseline(truth: list[set[str]], labels: list[str]) -> dict[str, Any]:
    """Reference points: predicting nothing, and predicting every label."""
    always_none = metrics.micro_macro_f1([set() for _ in truth], truth, labels)
    always_all = metrics.micro_macro_f1([set(labels) for _ in truth], truth, labels)
    return {
        "always_negative": {"micro_f1": always_none["micro_f1"], "macro_f1": always_none["macro_f1"]},
        "always_every_label": {"micro_f1": always_all["micro_f1"], "macro_f1": always_all["macro_f1"]},
        "rows_with_any_label": sum(bool(t) for t in truth),
    }


def main() -> int:
    rows = load()
    support: dict[str, int] = {}
    for row in rows:
        for label in row["sections"]:
            support[label] = support.get(label, 0) + 1
    labels = sorted((label for label, count in support.items() if count >= MIN_SUPPORT), key=lambda l: -support[l])
    truth = [r["sections"] & set(labels) for r in rows]

    variants: dict[str, dict[str, Any]] = {}
    scores_by_variant: dict[str, np.ndarray] = {}
    for variant in VARIANTS:
        scores = oof(variant, rows, labels)
        scores_by_variant[variant] = scores
        variants[variant] = evaluate_variant(variant, scores, truth, labels)

    best_variant = max(variants, key=lambda v: variants[v]["macro_ap"])
    nested = nested_threshold(rows, labels, best_variant)
    honest = nested_variant_selection(rows, labels)
    calibration = cross_fitted_calibration(rows, scores_by_variant[best_variant], labels)
    base_rate = sum(support[label] for label in labels) / (len(labels) * len(rows))
    ap_lift = variants[best_variant]["macro_ap"] / base_rate if base_rate else 0.0

    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "task": "multi-label exemption section prediction",
        "text_fields": list(TEXT_FIELDS),
        "leakage_note": "instruction/response excluded: they quote the decision and name the section",
        "labels": labels,
        "label_support": {label: support[label] for label in labels},
        "min_support": MIN_SUPPORT,
        "rows_evaluated": len(rows),
        "rows_multilabel": sum(len(r["sections"]) > 1 for r in rows),
        "evaluation": "5-fold CV, folds by hf_id % 5, pooled out-of-fold",
        "variants": variants,
        "best_variant": best_variant,
        "nested_threshold": nested,
        "nested_variant_selection": honest,
        "calibration": calibration,
        "baseline": baseline(truth, labels),
        "gate": {
            "macro_ap": variants[best_variant]["macro_ap"],
            "mean_base_rate": round(base_rate, 4),
            "ap_lift": round(ap_lift, 4),
        },
        "note": (
            "Labels come from CIC appellate decisions, not citizen requests. "
            "Macro AP is the headline because it is threshold-free. At 0.5 the model "
            "is a screening aid only; no benchmark here supports blocking a filing."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"rows={len(rows)} multi-label={results['rows_multilabel']} labels={len(labels)}")
    for variant, data in variants.items():
        print(
            f"[{variant:<5}] macroAP={data['macro_ap']} macroAUC={data['macro_roc_auc']} "
            f"microF1@0.5={data['at_0.5']['micro_f1']} macroF1@0.5={data['at_0.5']['macro_f1']} "
            f"brier={data['mean_brier']} ece={data['mean_ece']}"
        )
    print(f"best variant by macro AP: {best_variant}")
    print(f"nested: {nested}")
    print(f"nested variant selection: {honest}")
    print(
        f"calibration: ECE {calibration['ece_before']} -> {calibration['ece_after']}, "
        f"Brier {calibration['brier_before']} -> {calibration['brier_after']}"
    )
    print(f"gate: macro AP {variants[best_variant]['macro_ap']} vs base rate {round(base_rate, 4)} (lift {round(ap_lift, 2)}x)")
    print(f"baselines: {results['baseline']}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
