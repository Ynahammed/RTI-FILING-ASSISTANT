"""A synthetic development set is a smoke test, never evidence.

These tests keep that distinction mechanical: both dev files are complete and marked
synthetic, the dev scorer refuses anything unmarked and reports its own tuning
history, and `freeze` refuses to turn author-written rows into a benchmark split.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import annotate_citizen as annotate
from scripts import bench_citizen_dev as dev

DATA = Path("data/dev/synthetic-citizen.jsonl")
HOLDOUT = Path("data/dev/paraphrase-holdout.jsonl")


def test_the_development_set_is_complete_and_marked():
    rows = dev.load(DATA)
    assert len(rows) == 100
    assert sum(1 for row in rows if row["personal_data"]) == 35
    assert all(row["synthetic"] is True for row in rows)
    assert all(len(row["text"]) >= annotate.MIN_TEXT for row in rows)
    assert len({row["id"] for row in rows}) == 100


def test_the_development_set_covers_both_languages_and_edges():
    rows = dev.load(DATA)
    topics = {row["topic"] for row in rows}
    assert {"officer contact", "medical", "procurement", "own record", "grievance"} <= topics
    assert any("पुण्यातील" in row["text"] for row in rows)  # a Devanagari row

    # The three codebook edge cases must stay in the set, labelled as documented.
    by_id = {row["id"]: row for row in rows}
    assert by_id["SYN-056"]["personal_data"] is False  # officer's official approval
    assert by_id["SYN-057"]["personal_data"] is False  # designation and duties
    assert by_id["SYN-084"]["personal_data"] is False  # company accounts, 8(1)(d)
    assert by_id["SYN-094"]["personal_data"] is False  # own records


def test_the_paraphrase_holdout_is_complete_and_marked():
    rows = dev.load(HOLDOUT, dev.HOLDOUT_MIN_ROWS)
    assert len(rows) == 40
    assert sum(1 for row in rows if row["personal_data"]) == 20
    assert all(row["synthetic"] is True for row in rows)
    assert len({row["id"] for row in rows}) == 40
    # Sentences the first dev set did not contain: different register, other scripts.
    assert {row["language"] for row in rows} >= {"en", "mr"}
    assert any("पत्ता" in row["text"] for row in rows)


def test_the_holdout_is_scored_with_its_tuning_history_attached():
    scored = dev.score(dev.load(HOLDOUT, dev.HOLDOUT_MIN_ROWS))
    assert scored["rows"] == 40
    assert set(scored["rule_screen"]) >= {"precision", "recall", "f1"}
    assert isinstance(scored["rule_misses"], list)
    # The holdout was written after the first fix and then repaired against, so it is
    # in-sample too; the artifact must say so rather than imply a generalisation test.
    assert "in-sample" in dev.WARNING
    assert "Never quote" in dev.WARNING


def test_the_development_scorer_refuses_unmarked_rows(tmp_path):
    path = tmp_path / "dev.jsonl"
    row = {"id": "X-1", "text": "How much was spent on road repairs in ward 12?", "personal_data": False}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="synthetic"):
        dev.load(path)


def test_freeze_refuses_synthetic_rows(tmp_path):
    rows = dev.load(DATA)
    # Make it otherwise freezable: two labels that agree, consent, source.
    for row in rows:
        row.update({"consent": True, "source": "synthetic", "labels": {"A": row["personal_data"], "B": row["personal_data"]}})
    with pytest.raises(ValueError, match="synthetic rows cannot be frozen"):
        annotate.freeze(
            rows,
            frozen_path=tmp_path / "f.jsonl",
            manifest_path=tmp_path / "f.manifest.json",
        )
