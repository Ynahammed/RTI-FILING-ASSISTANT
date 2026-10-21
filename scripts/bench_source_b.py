"""Source B coverage and transfer benchmark for RTI-Bench.

Source A (`hf_annotated.csv`) has request-level text, so it supports screening
evaluation. Source B (`cic_combined.jsonl`) holds 298 full CIC orders with labels but
**no citizen request text**: the order narrates what happened. This script reports what
Source B can honestly support and what it cannot:

1. Corpus accounting, so the scored share of the 1,516-case corpus is explicit.
2. Label prevalence, which is a real measurement even without request text.
3. How often a request span can be extracted from an order (measured, not assumed).
4. A transfer test: a model trained on Source A request text, scored on Source B order
   text. A low score here quantifies the domain gap instead of hiding it.

It deliberately does not report a screening score for Source B, because the field that
would make one meaningful does not exist in it.

Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.bench_source_b
"""

from __future__ import annotations

import ast
import csv
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score

from agent import nlu
from common import metrics

SOURCE_A = Path("data/rti-bench/hf_annotated.csv")
SOURCE_B = Path("data/rti-bench/cic_combined.jsonl")
OUT = Path("artifacts/source_b_results.json")
REQUEST_PATTERNS = (
    r"(?:sought|seeking|requested)\s+(?:the\s+)?information[^.]{0,300}",
    r"information\s+sought[^.]{0,300}",
    r"(?:appellant|applicant|complainant)\s+(?:had\s+)?(?:sought|requested|filed)[^.]{0,300}",
)


def sections_of(raw: Any) -> list[str]:
    """Accept a list (JSONL) or a stringified list (CSV)."""
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    try:
        cited = ast.literal_eval(raw or "[]")
    except (ValueError, SyntaxError):
        return []
    return [str(item) for item in cited] if isinstance(cited, list) else []


def load_a() -> tuple[int, list[dict[str, Any]]]:
    """Return (all rows, scored rows). UNKNOWN outcomes are excluded from scoring."""
    with SOURCE_A.open(encoding="utf-8", newline="") as fh:
        all_rows = list(csv.DictReader(fh))
    scored = [r for r in all_rows if r["outcome"] != "UNKNOWN"]
    for row in scored:
        row["sections"] = sections_of(row.get("exemptions_cited", ""))
    return len(all_rows), scored


def load_b() -> list[dict[str, Any]]:
    records = [json.loads(line) for line in SOURCE_B.open(encoding="utf-8") if line.strip()]
    for record in records:
        record["sections"] = sections_of(record.get("exemptions_cited", "[]"))
        record["text"] = " ".join(
            part for part in (record.get("original_order_text"), record.get("adjunct_ruling_text")) if part
        ).strip()
    return records


def request_span(text: str) -> str | None:
    for pattern in REQUEST_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def citation_detection(rows: list[dict[str, Any]], label: str = "8(1)(j)") -> dict[str, Any]:
    """Out-of-fold detection of a cited section from order text.

    This is a real task for ingesting past orders, not a screening score. The
    dataset labels were extracted from the same documents by a rule pipeline, so
    these numbers are inflated by construction and are labelled as such.
    """
    y = np.array([1 if label in r["sections"] else 0 for r in rows])
    texts = [r["text"] for r in rows]
    scores = np.zeros(len(rows))
    for k in range(5):
        test = [i for i in range(len(rows)) if i % 5 == k]
        train = [i for i in range(len(rows)) if i % 5 != k]
        if not test or not train or y[train].sum() == 0:
            continue
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
        x_train = vec.fit_transform([texts[i] for i in train])
        x_test = vec.transform([texts[i] for i in test])
        clf = LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000).fit(x_train, y[train])
        scores[test] = clf.predict_proba(x_test)[:, 1]
    auc = float(roc_auc_score(y, scores)) if 0 < y.sum() < len(y) else None
    ap = float(average_precision_score(y, scores))
    pred = [bool(s >= 0.5) for s in scores]
    truth = [bool(v) for v in y]
    return {
        "task": f"detect whether an order cites {label}",
        "rows": len(rows),
        "positives": int(y.sum()),
        "roc_auc": round(auc, 4) if auc is not None else None,
        "average_precision": round(ap, 4),
        "at_0.5": metrics.prf(
            sum(p and t for p, t in zip(pred, truth)),
            sum(p and not t for p, t in zip(pred, truth)),
            sum((not p) and t for p, t in zip(pred, truth)),
        ),
        "leakage_note": (
            "labels were extracted from these documents by a rule pipeline; "
            "scores are inflated by construction"
        ),
    }


def main() -> int:
    a_total, a = load_a()
    b = load_b()

    labelled_b = [r for r in b if r["sections"]]
    prevalence: dict[str, int] = {}
    for record in b:
        for section in record["sections"]:
            prevalence[section] = prevalence.get(section, 0) + 1

    # The downloaded Source B is metadata-complete but text-incomplete: the CSV declares
    # text_length of thousands of characters while the JSONL carries text for few rows.
    with_text = [r for r in b if len(r["text"].strip()) > 0]
    extractable = [r for r in with_text if request_span(r["text"])]
    positive_b = [1 if "8(1)(j)" in r["sections"] else 0 for r in b]
    positives_with_text = sum(1 for r in with_text if "8(1)(j)" in r["sections"])
    sufficient_text = len(with_text) >= 50 and positives_with_text >= 10

    # Transfer and citation tests run only where text exists; below the floor they are
    # reported as insufficient rather than as a chance-level score that looks like a model.
    if sufficient_text:
        positive_subset = [1 if "8(1)(j)" in r["sections"] else 0 for r in with_text]
        trainer = [r for r in a if (r.get("title") or "").strip()]
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
        x_train = vec.fit_transform(
            [f"{r.get('title') or ''} {r.get('information_sought') or ''}".strip() for r in trainer]
        )
        y_train = np.array([1 if "8(1)(j)" in r["sections"] else 0 for r in trainer])
        clf = LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000).fit(x_train, y_train)
        scores = clf.predict_proba(vec.transform([r["text"] for r in with_text]))[:, 1]
        transfer = {
            "rows": len(with_text),
            "positives": positives_with_text,
            "roc_auc": round(float(roc_auc_score(positive_subset, scores)), 4),
            "average_precision": round(float(average_precision_score(positive_subset, scores)), 4),
            "base_rate": round(sum(positive_subset) / len(positive_subset), 4),
            "note": "trained on Source A request text, scored on Source B order text",
        }
    else:
        transfer = {
            "rows": len(with_text),
            "positives": positives_with_text,
            "insufficient": True,
            "note": (
                "too few Source B records carry order text to score a transfer test; "
                "any AUC here would describe the download, not the model"
            ),
        }

    citation = (
        citation_detection(with_text)
        if sufficient_text
        else {
            "rows": len(with_text),
            "insufficient": True,
            "note": "not enough records with order text to train a citation detector",
        }
    )

    keyword_pairs = [
        (nlu.has_personal_data(r["text"]), bool(1 if "8(1)(j)" in r["sections"] else 0))
        for r in with_text
    ]
    kw = metrics.prf(
        sum(p and t for p, t in keyword_pairs),
        sum(p and not t for p, t in keyword_pairs),
        sum((not p) and t for p, t in keyword_pairs),
    )

    total = a_total + len(b)
    results = {
        "dataset": "joyboseroy/rti-bench",
        "corpus": {
            "total_cases": total,
            "source_a_rows": a_total,
            "source_a_scored": len(a),
            "source_a_excluded_unknown": a_total - len(a),
            "source_b_documents": len(b),
            "scored_share_of_corpus": round(len(a) / total, 4),
            "examined_share_of_corpus": round((len(a) + len(b)) / total, 4),
        },
        "source_b": {
            "documents": len(b),
            "documents_with_any_section": len(labelled_b),
            "documents_citing_8_1_j": sum(positive_b),
            "prevalence": dict(sorted(prevalence.items(), key=lambda kv: -kv[1])),
            "documents_with_text": len(with_text),
            "text_completeness": round(len(with_text) / len(b), 4),
            "request_spans_extractable": len(extractable),
            "request_span_coverage": round(len(extractable) / len(b), 4),
            "text_sufficient_for_evaluation": sufficient_text,
            "note": (
                "the CSV declares text_length of thousands of characters but the JSONL "
                "carries text for only "
                f"{len(with_text)} of {len(b)} records; re-fetch the full text before "
                "any text-based Source B evaluation"
            ),
        },
        "transfer": transfer,
        "citation_detection": citation,
        "keyword_screen_on_orders": {
            **kw,
            "note": "order text narrates the decision; not a screening score",
        },
        "why_source_b_is_not_screened": (
            "Source B contains CIC orders, not requests, and the downloaded copy carries "
            f"text for only {len(with_text)} of {len(b)} records. A screening score on this "
            "source would measure the order's narration at best and the download at worst, "
            "so it is reported as not scoreable until the full text is fetched."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"corpus={total} source_a={a_total} scored={len(a)} source_b={len(b)} "
        f"scored_share={results['corpus']['scored_share_of_corpus']}"
    )
    print(
        f"source B: labelled={len(labelled_b)} 8(1)(j)={sum(positive_b)} "
        f"docs_with_text={len(with_text)}/{len(b)} "
        f"request_spans={len(extractable)} ({results['source_b']['request_span_coverage']:.1%})"
    )
    print(f"transfer: {transfer}")
    print(f"citation detection: {citation}")
    print(f"keyword screen on orders: {kw}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
