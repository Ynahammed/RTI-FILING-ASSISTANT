"""Benchmark the Section 8(1)(j) personal-data screen against RTI-Bench (Source A).

The screen under test is agent.nlu.has_personal_data, the same rule that triage uses
to fire the 8(1)(j) block. Ground truth is whether the decision cites 8(1)(j).
Cases with an UNKNOWN outcome are excluded, as the dataset card recommends for
human-review labels.

Usage (from the project root, after downloading the dataset to data/rti-bench):
    python -m scripts.bench_rti_bench
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from agent import nlu

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT = Path("artifacts/rti_bench_results.json")
FIELDS = ("information_sought", "instruction")
# `instruction` quotes the PIO reply and the decision, which name 8(1)(j) in most positives.
# Scores on it measure label leakage, not the screen, so they are never a real result.
LEAKY_FIELDS = {"instruction"}
LEAKY_NOTE = "label leakage: field quotes the decision; not a real result"
EXAMPLES = 10


def load(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def metrics(tp: int, fp: int, fn: int, tn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def run_field(rows: list[dict[str, str]], field: str) -> dict[str, object]:
    tp = fp = fn = tn = 0
    misses: list[dict[str, str]] = []
    false_alarms: list[dict[str, str]] = []
    for row in rows:
        text = (row.get(field) or "").strip()
        predicted = bool(text) and nlu.has_personal_data(text)
        actual = "8(1)(j)" in (row.get("exemptions_cited") or "")
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
            if len(false_alarms) < EXAMPLES:
                false_alarms.append({"hf_id": row["hf_id"], "text": text[:200]})
        elif actual:
            fn += 1
            if len(misses) < EXAMPLES:
                misses.append({"hf_id": row["hf_id"], "text": text[:200]})
        else:
            tn += 1
    return {
        "field": field,
        "leaky": field in LEAKY_FIELDS,
        "note": LEAKY_NOTE if field in LEAKY_FIELDS else "",
        **metrics(tp, fp, fn, tn),
        "sample_misses": misses,
        "sample_false_alarms": false_alarms,
    }


def main() -> int:
    rows = load(DATA)
    kept = [r for r in rows if r["outcome"] != "UNKNOWN"]
    results = {
        "dataset": "joyboseroy/rti-bench (hf_annotated.csv, Source A)",
        "rows_total": len(rows),
        "rows_scored": len(kept),
        "rows_excluded_unknown": len(rows) - len(kept),
        "positives_8_1_j": sum("8(1)(j)" in (r["exemptions_cited"] or "") for r in kept),
        "fields": [run_field(kept, field) for field in FIELDS],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Scored {results['rows_scored']} cases (excluded {results['rows_excluded_unknown']} UNKNOWN)")
    print(f"Cases citing 8(1)(j): {results['positives_8_1_j']}")
    for entry in results["fields"]:
        tag = " LEAKY: " + LEAKY_NOTE if entry["leaky"] else ""
        print(
            f"[{entry['field']}] tp={entry['tp']} fp={entry['fp']} fn={entry['fn']} tn={entry['tn']} "
            f"precision={entry['precision']} recall={entry['recall']} f1={entry['f1']}{tag}"
        )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
