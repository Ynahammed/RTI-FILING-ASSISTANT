"""Train and evaluate the advisory Section 8(1)(j) Naive Bayes model on RTI-Bench.

Input text is the `information_sought` field only. The `instruction` field is not
used because it contains the cited exemption in about 10% of rows, which leaks the label.
Rows with blank `information_sought` are excluded: no text model can score them.

Evaluation: 5-fold cross-validation, folds by hf_id % 5. Each fold is scored by a model
trained on the other four folds. Metrics are pooled over the out-of-fold predictions.
The threshold is reported both at the shipped value and as the best value on the pooled
out-of-fold sweep (the latter is optimistic, because it is chosen on the same rows).

The shipped model is trained on all scored rows with text, and written to rules/
(the agent reads it from there, and the Docker image copies rules/). Results go to artifacts/.

Usage (from the project root, after downloading the dataset to data/rti-bench):
    python -m scripts.train_8_1_j_classifier
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from agent import nlu
from agent.exemption_model import ALPHA, MODEL_PATH, posterior, tokenize

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT_DIR = Path("artifacts")
RESULTS = OUT_DIR / "classifier_8_1_j_results.json"
FIELD = "information_sought"
SHIPPED_THRESHOLD = 0.4
THRESHOLDS = [round(t / 100, 2) for t in range(5, 96, 5)]


def load(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def train(rows: list[dict[str, str]]) -> dict[str, Any]:
    docs = [(tokenize(r[FIELD]), r["label"] == "1") for r in rows]
    pos_docs = sum(label for _, label in docs)
    neg_docs = len(docs) - pos_docs
    pos_counts: Counter[str] = Counter()
    neg_counts: Counter[str] = Counter()
    for tokens, label in docs:
        (pos_counts if label else neg_counts).update(tokens)
    vocab = set(pos_counts) | set(neg_counts)
    return {
        "log_prior_pos": math.log(pos_docs / len(docs)),
        "log_prior_neg": math.log(neg_docs / len(docs)),
        "pos_counts": dict(pos_counts),
        "neg_counts": dict(neg_counts),
        "pos_total": sum(pos_counts.values()),
        "neg_total": sum(neg_counts.values()),
        "vocab_size": len(vocab),
    }


def metrics(pairs: list[tuple[bool, bool]]) -> dict[str, float | int]:
    tp = sum(p and a for p, a in pairs)
    fp = sum(p and not a for p, a in pairs)
    fn = sum((not p) and a for p, a in pairs)
    tn = sum((not p) and (not a) for p, a in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def main() -> int:
    rows = [r for r in load(DATA) if r["outcome"] != "UNKNOWN" and r[FIELD].strip()]
    for r in rows:
        r["label"] = "1" if "8(1)(j)" in (r["exemptions_cited"] or "") else "0"
        r["fold"] = str(int(r["hf_id"]) % 5)

    oof: list[tuple[float, bool, dict[str, str]]] = []
    for k in range(5):
        train_rows = [r for r in rows if r["fold"] != str(k)]
        test_rows = [r for r in rows if r["fold"] == str(k)]
        model = train(train_rows)
        for r in test_rows:
            oof.append((posterior(model, r[FIELD]), r["label"] == "1", r))

    sweep = []
    for t in THRESHOLDS:
        sweep.append({"threshold": t, **metrics([(s >= t, a) for s, a, _ in oof])})
    best = max(sweep, key=lambda m: m["f1"])
    clf_shipped = metrics([(s >= SHIPPED_THRESHOLD, a) for s, a, _ in oof])
    kw = metrics([(nlu.has_personal_data(r[FIELD]), a) for _, a, r in oof])

    final_model = train(rows)
    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "input_field": FIELD,
        "label": "exemptions_cited contains 8(1)(j)",
        "excluded": "outcome == UNKNOWN, and blank information_sought",
        "smoothing_alpha": ALPHA,
        "evaluation": "5-fold cross-validation, folds by hf_id % 5, pooled out-of-fold",
        "rows_evaluated": len(oof),
        "positives_evaluated": sum(a for _, a, _ in oof),
        "classifier": {
            "shipped_threshold": SHIPPED_THRESHOLD,
            "oof_at_shipped_threshold": clf_shipped,
            "best_threshold_on_oof_sweep (optimistic)": best,
            "sweep": sweep,
        },
        "keyword_screen": {"oof_pooled_rows": kw},
        "note": (
            "Text is the RTI-Bench request field. Advisory runs on the citizen narrative, which "
            "this model was not trained on; see README/report for the transfer check."
        ),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(
        json.dumps({"threshold": SHIPPED_THRESHOLD, "field": FIELD, **final_model}, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"rows evaluated: {len(oof)} (positives {results['positives_evaluated']})")
    print("classifier OOF @0.4:", clf_shipped)
    print("classifier best OOF sweep (optimistic):", best)
    print("keyword screen OOF:", kw)
    print(f"Wrote {RESULTS} and {MODEL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
