"""Precedent retrieval benchmark on RTI-Bench (Source A).

The dashboard already offers "similar past requests". This measures whether that
retrieval is actually useful, using the corpus itself as ground truth: a retrieved
case is relevant when it cites the same exemption section(s) as the query case.

Two relevance definitions are scored, because they answer different questions:
* `any_section` — shares at least one exemption section (a useful precedent).
* `same_primary` — shares the first cited section (a like-for-like precedent).

Retrieval is unsupervised (TF-IDF cosine), so there is no train/test split to leak;
each case is queried against every other case, and the case itself is excluded.

Baseline: a random ranking's expected precision@k is the mean share of relevant
cases in the corpus, so the lift over that baseline is the honest signal.

Usage (project root, after downloading the dataset to data/rti-bench):
    python -m scripts.bench_retrieval
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
from sklearn.metrics.pairwise import linear_kernel

from common import metrics

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT = Path("artifacts/retrieval_results.json")
TEXT_FIELDS = ("title", "information_sought")
KS = (1, 5, 10, 20)


def load() -> list[dict[str, Any]]:
    with DATA.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["outcome"] != "UNKNOWN"]
    for row in rows:
        try:
            cited = ast.literal_eval(row.get("exemptions_cited") or "[]")
        except (ValueError, SyntaxError):
            cited = []
        cited = cited if isinstance(cited, list) else []
        row["sections"] = set(cited)
        row["primary"] = cited[0] if cited else ""
        row["authority"] = (row.get("public_authority") or "").strip().lower()
    return rows


def text_of(row: dict[str, Any]) -> str:
    return " ".join((row.get(f) or "").strip() for f in TEXT_FIELDS).strip()


def similarity_matrix(texts: list[str]):
    """Cosine similarity over TF-IDF word and character features."""
    word = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
    char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=2)
    x = hstack([word.fit_transform(texts), char.fit_transform(texts)]).tocsr()
    sim = linear_kernel(x, x)
    np.fill_diagonal(sim, -1.0)  # never retrieve the query itself
    return sim


def ranked_ids(sim_row: np.ndarray, ids: list[str], k: int) -> list[str]:
    order = np.argsort(-sim_row, kind="stable")[:k]
    return [ids[i] for i in order]


def evaluate(rows: list[dict[str, Any]], key: str, sim: np.ndarray) -> dict[str, Any]:
    ids = [str(r["hf_id"]) for r in rows]
    index = {rid: i for i, rid in enumerate(ids)}
    k_max = max(KS)
    totals = {f"recall@{k}": [] for k in KS}
    totals.update({f"precision@{k}": [] for k in KS})
    totals["mrr@10"] = []
    totals["ndcg@10"] = []
    chance_precision = []
    hit_at_1 = 0
    queries = 0

    for i, row in enumerate(rows):
        relevant = {
            str(other["hf_id"])
            for j, other in enumerate(rows)
            if j != i and _is_relevant(row, other, key)
        }
        if not relevant:
            continue
        queries += 1
        ranked = ranked_ids(sim[i], ids, k_max)
        for k in KS:
            totals[f"recall@{k}"].append(metrics.recall_at_k(ranked, relevant, k))
            totals[f"precision@{k}"].append(metrics.precision_at_k(ranked, relevant, k))
        totals["mrr@10"].append(metrics.mrr(ranked, relevant, k=10))
        totals["ndcg@10"].append(metrics.ndcg_at_k(ranked, relevant, 10))
        chance_precision.append(len(relevant) / (len(rows) - 1))
        hit_at_1 += ranked[0] in relevant

    return {
        "relevance": key,
        "queries_with_relevant_cases": queries,
        "recall_at_k": {k: metrics.mean(totals[f"recall@{k}"]) for k in KS},
        "precision_at_k": {k: metrics.mean(totals[f"precision@{k}"]) for k in KS},
        "mrr_at_10": metrics.mean(totals["mrr@10"]),
        "ndcg_at_10": metrics.mean(totals["ndcg@10"]),
        "top1_hit_rate": round(hit_at_1 / queries, 4) if queries else 0.0,
        "chance_precision_at_10": metrics.mean(chance_precision),
    }


def _is_relevant(query: dict[str, Any], other: dict[str, Any], key: str) -> bool:
    if key == "any_section":
        return bool(query["sections"]) and bool(query["sections"] & other["sections"])
    if key == "same_primary":
        return bool(query["primary"]) and query["primary"] == other["primary"]
    if key == "same_authority":
        return bool(query["authority"]) and query["authority"] == other["authority"]
    if key == "section_and_authority":
        return (
            _is_relevant(query, other, "any_section")
            and bool(query["authority"])
            and query["authority"] == other["authority"]
        )
    raise ValueError(f"unknown relevance key: {key}")


def results_preview(rows: list[dict[str, Any]], sim) -> list[dict[str, Any]]:
    """Score every relevance definition; named so main() can reuse the numbers."""
    return [
        evaluate(rows, key, sim)
        for key in ("any_section", "same_primary", "same_authority", "section_and_authority")
    ]


def main() -> int:
    rows = load()
    sim = similarity_matrix([text_of(r) for r in rows])
    scored = results_preview(rows, sim)
    any_section = next(e for e in scored if e["relevance"] == "any_section")
    by_authority = next(e for e in scored if e["relevance"] == "same_authority")
    authority_lift = (
        by_authority["precision_at_k"][10] / by_authority["chance_precision_at_10"]
        if by_authority["chance_precision_at_10"]
        else 0.0
    )
    lift = (
        any_section["precision_at_k"][10] / any_section["chance_precision_at_10"]
        if any_section["chance_precision_at_10"]
        else 0.0
    )
    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "method": "TF-IDF word(1,2) + char_wb(3,5), cosine similarity, self excluded",
        "rows": len(rows),
        "rows_with_sections": sum(bool(r["sections"]) for r in rows),
        "k": list(KS),
        "relevance": scored,
        "relevance_definitions": {
            "any_section": "shares at least one exemption section",
            "same_primary": "shares the first cited section",
            "same_authority": "same public authority",
            "section_and_authority": "shares a section and the same authority",
        },
        "gate": {
            "precision_at_10": any_section["precision_at_k"][10],
            "chance_precision_at_10": any_section["chance_precision_at_10"],
            "precision_lift": round(lift, 4),
            "authority_precision_lift": round(authority_lift, 4),
        },
        "note": (
            "Retrieval is unsupervised, so no fold split is needed. Precedents are "
            "relevance-labelled by exemption section, which is a proxy for usefulness: "
            "two cases can share a section without sharing an authority or remedy."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"rows={len(rows)} with_sections={results['rows_with_sections']}")
    for entry in results["relevance"]:
        print(
            f"[{entry['relevance']:<12}] queries={entry['queries_with_relevant_cases']} "
            f"R@10={entry['recall_at_k'][10]} P@10={entry['precision_at_k'][10]} "
            f"MRR@10={entry['mrr_at_10']} nDCG@10={entry['ndcg_at_10']} "
            f"top1={entry['top1_hit_rate']} chance_P@10={entry['chance_precision_at_10']}"
        )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
