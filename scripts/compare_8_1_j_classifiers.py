"""Compare candidate Section 8(1)(j) text classifiers on RTI-Bench (Source A).

Exploration only: the shipped advisory model is still scripts/train_8_1_j_classifier.py.
Uses repeated stratified 5-fold CV and pooled out-of-fold scores, so every row is scored
by a model that did not train on it.

Leakage: `instruction` and `response` quote the PIO reply and the Commission's
decision, so they often name 8(1)(j) directly. They are excluded. Only fields that
describe the request (`title`, `information_sought`) are used.

Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.compare_8_1_j_classifiers
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import make_pipeline
from sklearn.svm import LinearSVC

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT = Path("artifacts/classifier_comparison.json")
SEEDS = (0, 1, 2)
FOLDS = 5


def load() -> list[dict[str, str]]:
    with DATA.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if r["outcome"] != "UNKNOWN"]


def label(row: dict[str, str]) -> int:
    return int("8(1)(j)" in (row.get("exemptions_cited") or ""))


def field_text(row: dict[str, str], field: str) -> str:
    if field == "title+information_sought":
        return f"{row.get('title') or ''} {row.get('information_sought') or ''}".strip()
    return (row.get(field) or "").strip()


MODELS = {
    "nb_counts": lambda: make_pipeline(CountVectorizer(lowercase=True), MultinomialNB(alpha=1.0)),
    "lr_word": lambda: make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1),
        LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000),
    ),
    "lr_char": lambda: make_pipeline(
        TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=2),
        LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000),
    ),
    "svm_word": lambda: make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1),
        LinearSVC(C=0.5, class_weight="balanced"),
    ),
}


def scores_for(model_name: str, texts: list[str], y: np.ndarray, seed: int) -> np.ndarray:
    """Out-of-fold scores for one seed (higher means more likely 8(1)(j))."""
    out = np.zeros(len(texts))
    skf = StratifiedKFold(n_splits=FOLDS, shuffle=True, random_state=seed)
    x = np.array(texts, dtype=object)
    for train, test in skf.split(x, y):
        model = MODELS[model_name]()
        model.fit(x[train], y[train])
        if hasattr(model, "predict_proba"):
            out[test] = model.predict_proba(x[test])[:, 1]
        else:
            out[test] = model.decision_function(x[test])
    return out


def best_f1(y: np.ndarray, s: np.ndarray) -> tuple[float, float]:
    precision, recall, _ = precision_recall_curve(y, s)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    idx = int(np.argmax(f1[:-1])) if len(f1) > 1 else 0
    return float(f1[idx]), float(recall[idx])


def evaluate(field: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    texts = [field_text(r, field) for r in rows]
    y = np.array([label(r) for r in rows])
    results = []
    for name in MODELS:
        aucs, aps, f1s = [], [], []
        for seed in SEEDS:
            s = scores_for(name, texts, y, seed)
            aucs.append(roc_auc_score(y, s))
            aps.append(average_precision_score(y, s))
            f1s.append(best_f1(y, s)[0])
        results.append(
            {
                "field": field,
                "model": name,
                "rows": len(rows),
                "positives": int(y.sum()),
                "base_rate": round(float(y.mean()), 4),
                "roc_auc_mean": round(float(np.mean(aucs)), 4),
                "avg_precision_mean": round(float(np.mean(aps)), 4),
                "best_f1_mean": round(float(np.mean(f1s)), 4),
            }
        )
    return results


def main() -> int:
    rows = load()
    all_rows = rows
    with_info = [r for r in rows if (r.get("information_sought") or "").strip()]
    jobs = [
        ("title", all_rows),
        ("title+information_sought", all_rows),
        ("information_sought", with_info),
    ]
    results: list[dict[str, object]] = []
    for field, subset in jobs:
        results.extend(evaluate(field, subset))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"folds": FOLDS, "seeds": list(SEEDS), "results": results}, indent=2), encoding="utf-8")
    for r in results:
        print(
            f"{r['field']:<26} {r['model']:<10} n={r['rows']} pos={r['positives']} "
            f"AUC={r['roc_auc_mean']} AP={r['avg_precision_mean']} (base {r['base_rate']}) bestF1={r['best_f1_mean']}"
        )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
