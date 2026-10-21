"""Evaluate routing suspected Section 8(1)(j) requests to a human review queue.

The deterministic rule is precise but very conservative: on RTI-Bench it catches 9 of
119 8(1)(j) cases (recall 0.076), so most requests that an appellate body later found
exempt are filed. The classifier is too imprecise to block anything, but it is good
enough to *route*: a flagged request goes to a human instead of being filed
automatically, and it is not rejected.

This script measures the combined screen — rule OR calibrated classifier >= threshold —
out of fold, and selects the threshold under an explicit workload budget rather than
the F1-optimal cut:

    maximise recall subject to precision >= 0.28 and review load <= 16%

It then repeats the selection inside each outer fold (threshold chosen on the other
four folds only) so the reported numbers are not the product of tuning on the rows
being scored. Writes artifacts/review_routing_results.json.

Usage (project root, needs scikit-learn from requirements-dev.txt):
    python -m scripts.bench_review_routing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from agent import nlu
from scripts.train_8_1_j_classifier import DATA, load, metrics, oof_scores_for
from common import metrics as common_metrics

OUT = Path("artifacts/review_routing_results.json")
THRESHOLDS = [round(t / 100, 2) for t in range(5, 96, 1)]
MIN_PRECISION = 0.28
MAX_REVIEW_LOAD = 0.16
MIN_RECALL = 0.35


def prepare() -> list[dict[str, str]]:
    rows = [r for r in load(DATA) if r["outcome"] != "UNKNOWN"]
    for row in rows:
        row["label"] = "1" if "8(1)(j)" in (row["exemptions_cited"] or "") else "0"
        row["fold"] = str(int(row["hf_id"]) % 5)
    return rows


def evaluate(predicted: np.ndarray, labels: np.ndarray, index: np.ndarray) -> dict[str, Any]:
    result = metrics(list(zip(predicted[index], labels[index])))
    result["review_load"] = round(float(predicted[index].mean()), 4)
    result["rows"] = int(len(index))
    return result


def choose(
    scores: np.ndarray, rule: np.ndarray, labels: np.ndarray, index: np.ndarray
) -> dict[str, Any] | None:
    """Max recall under the workload budget, on the rows in `index` only."""
    best: dict[str, Any] | None = None
    for threshold in THRESHOLDS:
        predicted = rule | (scores >= threshold)
        result = evaluate(predicted, labels, index)
        if result["precision"] < MIN_PRECISION or result["review_load"] > MAX_REVIEW_LOAD:
            continue
        if best is None or result["recall"] > best["metrics"]["recall"]:
            best = {"threshold": threshold, "metrics": result}
        elif result["recall"] == best["metrics"]["recall"] and result["precision"] > best["metrics"]["precision"]:
            best = {"threshold": threshold, "metrics": result}
    return best


def nested(
    scores: np.ndarray, rule: np.ndarray, labels: np.ndarray, rows: list[dict[str, str]]
) -> dict[str, Any]:
    """Choose the threshold without the scored rows, then pool the held-out folds."""
    predicted_all: list[np.ndarray] = []
    labels_all: list[np.ndarray] = []
    chosen: list[float] = []
    for k in range(5):
        outer = np.array([i for i, r in enumerate(rows) if r["fold"] == str(k)])
        inner = np.array([i for i, r in enumerate(rows) if r["fold"] != str(k)])
        choice = choose(scores, rule, labels, inner)
        if choice is None:
            continue
        chosen.append(choice["threshold"])
        predicted_all.append((rule | (scores >= choice["threshold"]))[outer])
        labels_all.append(labels[outer])
    if not predicted_all:
        return {"evaluated": False, "reason": "no threshold met the workload budget"}
    predicted = np.concatenate(predicted_all)
    truth = np.concatenate(labels_all)
    pooled = metrics(list(zip(predicted, truth)))
    pooled["review_load"] = round(float(predicted.mean()), 4)
    return {
        "evaluated": True,
        "threshold_per_outer_fold": chosen,
        "pooled_metrics": pooled,
    }


def main() -> int:
    rows = prepare()
    scores = oof_scores_for(rows)
    labels = np.array([r["label"] == "1" for r in rows])
    rule = np.array([nlu.has_personal_data(r["information_sought"] or "") for r in rows])
    everything = np.arange(len(rows))

    rule_only = evaluate(rule, labels, everything)
    choice = choose(scores, rule, labels, everything)
    if choice is None:
        raise SystemExit("no threshold met the review workload budget")

    threshold = choice["threshold"]
    pooled = evaluate(rule | (scores >= threshold), labels, everything)
    honest = nested(scores, rule, labels, rows)
    sweep = [
        {
            "threshold": t,
            **evaluate(rule | (scores >= t), labels, everything),
        }
        for t in THRESHOLDS
    ]
    recall_lift = pooled["recall"] / rule_only["recall"] if rule_only["recall"] else 0.0

    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "task": "route suspected 8(1)(j) requests to human review (not block)",
        "rows_evaluated": len(rows),
        "positives": int(labels.sum()),
        "policy": {
            "rule": f"max recall subject to precision >= {MIN_PRECISION} and review load <= {MAX_REVIEW_LOAD}",
            "selection_rows": "out-of-fold predictions; threshold chosen on all rows",
            "honest_check": "threshold re-chosen inside each outer fold",
        },
        "rule_only": rule_only,
        "chosen_threshold": threshold,
        "pooled": pooled,
        "recall_lift_over_rule": round(recall_lift, 2),
        "nested": honest,
        "sweep": sweep,
        "gate": {
            "threshold": threshold,
            "recall": pooled["recall"],
            "precision": pooled["precision"],
            "review_load": pooled["review_load"],
            "nested_recall": (honest.get("pooled_metrics") or {}).get("recall"),
            "min_recall": MIN_RECALL,
            "max_review_load": MAX_REVIEW_LOAD,
        },
        "note": (
            "A flagged request is reviewed by a human and is not filed automatically. "
            "It is not rejected. Precision here means the share of reviewed requests that "
            "carry an 8(1)(j) label in the appellate data; the rest are review cost, not "
            "wrongful refusals."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"rows={len(rows)} positives={int(labels.sum())}")
    print(f"rule only: {rule_only}")
    print(f"chosen threshold={threshold} pooled={pooled} lift={recall_lift:.2f}x")
    print(f"nested: {honest}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
