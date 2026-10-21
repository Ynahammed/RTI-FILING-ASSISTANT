"""Score the screening components on labelled citizen requests, when such data exists.

Every other benchmark in this repository uses RTI-Bench, which is built from appellate
decisions. That measures screening on decision-derived text, not on the phrasing citizens
actually use. Closing that gap needs labelled citizen requests, which cannot be
manufactured honestly.

The file must come from `scripts/annotate_citizen.py`, not from hand editing: it must
have a manifest whose SHA-256 matches the file, at least two annotators whose Cohen's
kappa clears 0.6, and at least 50 adjudicated rows. Without that, this benchmark reports
`present: false` and the reason, and scores nothing. The gate is the point: an unfrozen
or low-agreement file would produce a number that looks like evidence and is not.

Inputs (produced by the annotation pipeline):
    data/citizen-requests.jsonl
    data/citizen-requests.freeze.json

Usage (project root):
    python -m scripts.bench_citizen_requests
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent import exemption_model, nlu
from common import metrics
from scripts.annotate_citizen import (
    FROZEN,
    MANIFEST,
    MIN_KAPPA,
    TARGET_ROWS,
    load,
    progress,
    verify_frozen,
)

DATA = FROZEN
OUT = Path("artifacts/citizen_requests_results.json")
SCHEMA = {
    "text": "str, the citizen's request",
    "personal_data": "bool, adjudicated: whether the request seeks a third party's personal data",
    "sections": "list[str], optional: exemption sections a reviewer would cite",
    "split": "str, 'eval' for a frozen evaluation row",
}


def section_label(row: dict[str, Any]) -> int | None:
    if "personal_data" in row:
        return int(bool(row["personal_data"]))
    if "sections" in row:
        return int("8(1)(j)" in (row["sections"] or []))
    return None


def main() -> int:
    rows, manifest, reason = verify_frozen(DATA, MANIFEST)
    if rows is None:
        payload = {
            "present": False,
            "rows": 0,
            "reason": reason,
            "manifest": manifest,
            "schema": SCHEMA,
            "progress": progress(load()),
            "how_to_produce": (
                "python -m scripts.annotate_server  # contribute, label twice, adjudicate; "
                "then python -m scripts.annotate_citizen freeze"
            ),
            "why": (
                "RTI-Bench labels come from CIC decisions, so citizen-request accuracy stays "
                "unmeasured until an agreed, frozen file exists. Scoring an unfrozen or "
                "low-agreement set would report its author's opinion as accuracy."
            ),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"citizen requests: not scoreable — {reason}")
        print(f"Wrote {OUT}")
        return 0

    scored = [row for row in rows if section_label(row) is not None]
    labels = [int(section_label(row)) for row in scored]
    texts = [str(row["text"]) for row in scored]
    positives = sum(labels)

    keyword_pairs = [(nlu.has_personal_data(t), bool(y)) for t, y in zip(texts, labels)]
    tp = sum(p and y for p, y in keyword_pairs)
    fp = sum(p and not y for p, y in keyword_pairs)
    fn = sum((not p) and y for p, y in keyword_pairs)
    keyword = metrics.prf(tp, fp, fn)
    keyword["precision_ci95"] = list(metrics.wilson_interval(tp, tp + fp))
    keyword["recall_ci95"] = list(metrics.wilson_interval(tp, tp + fn))

    classifier: dict[str, Any] = {"evaluated": False, "reason": "model file not available"}
    model = exemption_model.load_model()
    if model is not None:
        scores = [exemption_model.probability(model, t) for t in texts]
        classifier = {
            "evaluated": True,
            "roc_auc": metrics.roc_auc(labels, scores),
            "average_precision": metrics.average_precision(labels, scores),
            "base_rate": round(positives / len(scored), 4) if scored else 0.0,
        }

    payload = {
        "present": True,
        "rows": len(scored),
        "positives": positives,
        "target_rows": TARGET_ROWS,
        "annotation": {
            "cohen_kappa": manifest.get("cohen_kappa"),
            "kappa_ci95": manifest.get("kappa_ci95"),
            "annotators": manifest.get("annotators"),
            "frozen_at": manifest.get("frozen_at"),
            "sha256": manifest.get("sha256"),
            "min_kappa": MIN_KAPPA,
        },
        "keyword_screen": keyword,
        "classifier": classifier,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"citizen requests: rows={len(scored)} positives={positives} kappa={manifest.get('cohen_kappa')}")
    print(f"keyword screen: {keyword}")
    print(f"classifier: {classifier}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
