"""Score the screening components on labelled citizen requests, when such data exists.

Every other benchmark in this repository uses RTI-Bench, which is built from appellate
decisions. That measures screening on decision-derived text, not on the phrasing citizens
actually use. Closing that gap needs labelled citizen requests, which cannot be
manufactured honestly, so this harness waits for real data instead of inventing it.

Input: `data/citizen-requests.jsonl`, one JSON object per line:

    {"text": "Please tell me how much was spent repairing the road near my house.",
     "personal_data": false}
    {"text": "I want the home address and phone number of the ward officer.",
     "personal_data": true}

`sections` may replace `personal_data` when the label is a section list:

    {"text": "...", "sections": ["8(1)(j)"]}

Without the file this script reports `present: false`, scores nothing, and exits 0: the
absence is a known data gap, not a failed build. With the file it scores the deterministic
screen and the shipped classifier and writes the numbers to artifacts/.

Usage (project root):
    python -m scripts.bench_citizen_requests
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent import exemption_model, nlu
from common import metrics

DATA = Path("data/citizen-requests.jsonl")
OUT = Path("artifacts/citizen_requests_results.json")
SCHEMA = {
    "text": "str, the citizen's request",
    "personal_data": "bool, optional: whether the request seeks a third party's personal data",
    "sections": "list[str], optional: exemption sections a reviewer would cite",
}


def load(path: Path = DATA) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def section_label(row: dict[str, Any]) -> int | None:
    if "personal_data" in row:
        return int(bool(row["personal_data"]))
    if "sections" in row:
        return int("8(1)(j)" in (row["sections"] or []))
    return None


def main() -> int:
    if not DATA.exists():
        payload = {
            "present": False,
            "rows": 0,
            "schema": SCHEMA,
            "why": (
                "No labelled citizen requests on disk. RTI-Bench labels come from CIC "
                "decisions, so citizen-request accuracy stays unmeasured until this file "
                "exists; scoring a synthetic set would only restate its author."
            ),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"citizen requests: not present ({DATA}); nothing scored")
        print(f"Wrote {OUT}")
        return 0

    rows = [r for r in load() if section_label(r) is not None]
    labels = [int(section_label(r)) for r in rows]
    texts = [str(r["text"]) for r in rows]
    positives = sum(labels)

    keyword_pairs = [(nlu.has_personal_data(t), bool(y)) for t, y in zip(texts, labels)]
    keyword = metrics.prf(
        sum(p and y for p, y in keyword_pairs),
        sum(p and not y for p, y in keyword_pairs),
        sum((not p) and y for p, y in keyword_pairs),
    )

    classifier: dict[str, Any] = {"evaluated": False, "reason": "model file not available"}
    model = exemption_model.load_model()
    if model is not None:
        scores = [exemption_model.probability(model, t) for t in texts]
        classifier = {
            "evaluated": True,
            "roc_auc": metrics.roc_auc(labels, scores),
            "average_precision": metrics.average_precision(labels, scores),
            "base_rate": round(positives / len(rows), 4) if rows else 0.0,
        }

    payload = {
        "present": True,
        "rows": len(rows),
        "positives": positives,
        "keyword_screen": keyword,
        "classifier": classifier,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"citizen requests: rows={len(rows)} positives={positives}")
    print(f"keyword screen: {keyword}")
    print(f"classifier: {classifier}")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
