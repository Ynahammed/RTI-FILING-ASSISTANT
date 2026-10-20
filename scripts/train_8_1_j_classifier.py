"""Train and evaluate the advisory Section 8(1)(j) text classifier on RTI-Bench.

Model: TF-IDF (word unigrams and bigrams, sublinear tf) with L2-regularised logistic
regression, chosen over Naive Bayes and linear SVM in scripts/compare_8_1_j_classifiers.py.

Scored text: `title` + `information_sought`. `instruction` and `response` are not used:
they quote the PIO reply and the Commission's decision and name 8(1)(j) in most positives,
so a model on them learns the decision, not the request.

Evaluation: 5-fold CV, folds by hf_id % 5, pooled out-of-fold predictions. The threshold is
chosen on the same out-of-fold sweep, so metrics at that threshold are optimistic.

The shipped model is trained on all scored rows. Its weights are written to rules/ and
scored in pure Python (agent/exemption_model.py), so the app needs no scikit-learn. This
script checks that the pure-Python scores match scikit-learn on every row before it writes.

Training needs scikit-learn, which is not in requirements.txt. Install it to retrain.
Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.train_8_1_j_classifier
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from agent import nlu
from agent.exemption_model import MODEL_PATH, NGRAM_RANGE, probability

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT_DIR = Path("artifacts")
RESULTS = OUT_DIR / "classifier_8_1_j_results.json"
TEXT_FIELDS = ("title", "information_sought")
C = 4.0
THRESHOLDS = [round(t / 100, 2) for t in range(5, 96, 1)]
TOLERANCE = 1e-6


def load(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def text_of(row: dict[str, str]) -> str:
    return " ".join((row.get(f) or "").strip() for f in TEXT_FIELDS).strip()


def fit(rows: list[dict[str, str]]) -> tuple[TfidfVectorizer, LogisticRegression]:
    vec = TfidfVectorizer(ngram_range=NGRAM_RANGE, sublinear_tf=True)
    x = vec.fit_transform([text_of(r) for r in rows])
    y = np.array([r["label"] == "1" for r in rows], dtype=int)
    clf = LogisticRegression(C=C, class_weight="balanced", max_iter=2000).fit(x, y)
    return vec, clf


def export(vec: TfidfVectorizer, clf: LogisticRegression, threshold: float) -> dict[str, Any]:
    return {
        "model": "tfidf_logreg",
        "text_fields": list(TEXT_FIELDS),
        "ngram_range": list(NGRAM_RANGE),
        "sublinear_tf": True,
        "vocabulary": {term: int(idx) for term, idx in vec.vocabulary_.items()},
        "idf": [float(v) for v in vec.idf_],
        "coef": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "C": C,
        "threshold": threshold,
    }


def metrics(pairs: list[tuple[bool, bool]]) -> dict[str, float | int]:
    pairs = [(bool(p), bool(a)) for p, a in pairs]
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
    rows = [r for r in load(DATA) if r["outcome"] != "UNKNOWN"]
    for r in rows:
        r["label"] = "1" if "8(1)(j)" in (r["exemptions_cited"] or "") else "0"
        r["fold"] = str(int(r["hf_id"]) % 5)

    oof_scores = np.zeros(len(rows))
    labels = np.array([r["label"] == "1" for r in rows])
    for k in range(5):
        train_idx = [i for i, r in enumerate(rows) if r["fold"] != str(k)]
        test_idx = [i for i, r in enumerate(rows) if r["fold"] == str(k)]
        vec, clf = fit([rows[i] for i in train_idx])
        x_test = vec.transform([text_of(rows[i]) for i in test_idx])
        oof_scores[test_idx] = clf.predict_proba(x_test)[:, 1]

    sweep = [
        {"threshold": t, **metrics(list(zip(oof_scores >= t, labels)))}
        for t in THRESHOLDS
    ]
    best = max(sweep, key=lambda m: m["f1"])
    threshold = float(best["threshold"])

    kw_pairs = [(nlu.has_personal_data(r["information_sought"] or ""), r["label"] == "1") for r in rows]
    vec, clf = fit(rows)
    model = export(vec, clf, threshold)

    # Check the pure-Python scorer against scikit-learn on every row.
    sk = clf.predict_proba(vec.transform([text_of(r) for r in rows]))[:, 1]
    py = np.array([probability(model, text_of(r)) for r in rows])
    worst = float(np.max(np.abs(sk - py)))
    if worst > TOLERANCE:
        raise SystemExit(f"pure-Python scorer differs from scikit-learn by {worst}")

    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "text_fields": list(TEXT_FIELDS),
        "label": "exemptions_cited contains 8(1)(j)",
        "excluded": "outcome == UNKNOWN",
        "model": {"type": "tfidf_logreg", "C": C, "ngram_range": list(NGRAM_RANGE)},
        "evaluation": "5-fold CV, folds by hf_id % 5, pooled out-of-fold",
        "rows_evaluated": len(rows),
        "positives_evaluated": int(labels.sum()),
        "shipped_threshold": threshold,
        "classifier_oof_at_chosen_threshold (optimistic: threshold chosen on same rows)": best,
        "classifier_sweep": sweep,
        "keyword_screen_oof_rows": metrics(kw_pairs),
        "pure_python_vs_sklearn_max_abs_diff": worst,
        "note": (
            "Advisory only, off by default (RTI_EXEMPTION_ADVISORY=1). At the chosen threshold, "
            f"out-of-fold precision is {best['precision']} against a base rate of "
            f"{round(float(labels.mean()), 3)}. The threshold was chosen on the same rows. "
            "It was not validated on real citizen-style text."
        ),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")

    print(f"rows evaluated: {len(rows)} (positives {int(labels.sum())})")
    print("threshold (best OOF F1, optimistic):", best)
    print("keyword screen (same rows):", results["keyword_screen_oof_rows"])
    print(f"pure-Python vs sklearn max abs diff: {worst:.2e}")
    print(f"Wrote {RESULTS} and {MODEL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
