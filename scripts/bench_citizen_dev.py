"""Development-only checks on synthetic citizen-style requests.

Why this file exists
--------------------
The honest citizen benchmark in `scripts/bench_citizen_requests.py` needs two human
annotators and real requests. This script is the opposite: the author wrote the
requests and the labels. It measures whether the screening looks broken on
citizen-register text and whether the pipeline runs end to end. It is **not**
evidence and can never be frozen:

* every row is marked `synthetic: true` and `freeze` refuses such rows;
* the artifact says `evidence: false` and carries the circularity warning;
* the real benchmark reads a different file, so these numbers cannot leak into it.

Two author-written files, for a reason:

* `data/dev/synthetic-citizen.jsonl` (100 rows) was written before the screen was
  changed. It showed the old rule at 0.14 recall, which is what prompted the work.
* `data/dev/paraphrase-holdout.jsonl` (40 rows) was written afterwards from the
  codebook, in different phrasings, and scored once: 0.55 recall at 0.79 precision
  with three false positives on public-office text.

The misses from both files have since been fixed, so **both are now in-sample**.
They are regression checks against phrasings the project has already seen, not a
measure of generalisation. Only the human-annotated, frozen set can be that. Quote
this script's numbers as "the screen handles these 140 sentences", never as
citizen accuracy.

    python -m scripts.bench_citizen_dev

Reads the two files above; writes `artifacts/citizen_dev_results.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent import exemption_model, nlu, rules
from common import metrics
from scripts.annotate_citizen import redact

DATA = Path("data/dev/synthetic-citizen.jsonl")
HOLDOUT = Path("data/dev/paraphrase-holdout.jsonl")
OUT = Path("artifacts/citizen_dev_results.json")
MIN_ROWS = 50
HOLDOUT_MIN_ROWS = 30
WARNING = (
    "Author-written and author-labelled, and the screen was repaired against these rows "
    "after scoring them, so both files are in-sample. Two independent annotators are not "
    "present, so kappa is undefined and accuracy here would describe the author's opinion, "
    "not citizen text. Never quote this as citizen accuracy."
)


def load(path: Path = DATA, min_rows: int = MIN_ROWS) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"missing development set: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(row.get("synthetic") is not True for row in rows):
        raise SystemExit("development set must be marked synthetic: true on every row")
    if len(rows) < min_rows:
        raise SystemExit(f"development set has {len(rows)} rows, need {min_rows}")
    if any("personal_data" not in row for row in rows):
        raise SystemExit("every development row needs a personal_data label")
    return rows


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(row["text"]) for row in rows]
    labels = [bool(row["personal_data"]) for row in rows]
    positives = sum(labels)

    rule = [nlu.has_personal_data(text) for text in texts]
    tp = sum(p and y for p, y in zip(rule, labels))
    fp = sum(p and not y for p, y in zip(rule, labels))
    fn = sum((not p) and y for p, y in zip(rule, labels))
    rule_metrics = metrics.prf(tp, fp, fn)
    rule_metrics["precision_ci95"] = list(metrics.wilson_interval(tp, tp + fp))
    rule_metrics["recall_ci95"] = list(metrics.wilson_interval(tp, tp + fn))
    misses = [str(row["id"]) for row, p, y in zip(rows, rule, labels) if p != y]

    classifier: dict[str, Any] = {"evaluated": False, "reason": "model file not available"}
    model = exemption_model.load_model()
    if model is not None:
        scores = [exemption_model.probability(model, text) for text in texts]
        threshold = rules.review_threshold()
        flagged = [bool(r) or s >= threshold for r, s in zip(rule, scores)]
        ftp = sum(p and y for p, y in zip(flagged, labels))
        ffp = sum(p and not y for p, y in zip(flagged, labels))
        ffn = sum((not p) and y for p, y in zip(flagged, labels))
        policy = metrics.prf(ftp, ffp, ffn)
        policy["review_load"] = round(sum(flagged) / len(flagged), 4)
        policy["recall_ci95"] = list(metrics.wilson_interval(ftp, ftp + ffn))
        policy["precision_ci95"] = list(metrics.wilson_interval(ftp, ftp + ffp))
        classifier = {
            "evaluated": True,
            "threshold": threshold,
            "roc_auc": metrics.roc_auc([int(y) for y in labels], scores),
            "average_precision": metrics.average_precision([int(y) for y in labels], scores),
            "base_rate": round(positives / len(rows), 4),
            "routing_policy": policy,
        }

    # Language mix matters: a dev set in one register cannot test multilingual claims.
    languages = {}
    for row in rows:
        key = str(row.get("language", "en"))
        languages[key] = languages.get(key, 0) + 1

    return {
        "rows": len(rows),
        "positives": positives,
        "negatives": len(rows) - positives,
        "languages": languages,
        "redactions_applied": sum(redact(str(row["text"]))[1] for row in rows),
        "rule_screen": rule_metrics,
        "rule_misses": misses,
        "classifier": classifier,
    }


def main() -> int:
    development = score(load(DATA))
    holdout = score(load(HOLDOUT, HOLDOUT_MIN_ROWS))
    payload = {
        "evidence": False,
        "warning": WARNING,
        "synthetic": True,
        "purpose": "regression check on author-written phrasings; not a citizen-accuracy measurement",
        "development": {"dataset": str(DATA), **development},
        "paraphrase_holdout": {
            "dataset": str(HOLDOUT),
            "history": (
                "Written after the screen was first extended and scored once at "
                "0.55 recall / 0.79 precision; the misses and false positives were then "
                "fixed, so this file is in-sample too."
            ),
            **holdout,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    for name, block in (("development", development), ("paraphrase holdout", holdout)):
        rule = block["rule_screen"]
        print(
            f"{name}: {block['rows']} rows, {block['positives']} positive — "
            f"rule precision {rule['precision']} recall {rule['recall']} (NOT evidence)"
        )
        if block["classifier"].get("evaluated"):
            policy = block["classifier"]["routing_policy"]
            print(
                f"  + classifier @ {block['classifier']['threshold']}: "
                f"precision {policy['precision']} recall {policy['recall']} "
                f"load {policy['review_load']}"
            )
        if block["rule_misses"]:
            print(f"  rule misses: {', '.join(block['rule_misses'])}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
