"""The citizen benchmark must refuse data that cannot support a claim."""

from __future__ import annotations

import json

import pytest

from common import metrics
from scripts import annotate_citizen as annotate


def row(index: int, a: bool, b: bool, *, adjudicated: bool | None = None) -> dict:
    item = {
        "id": f"CIT-{index:04d}",
        "text": f"Please provide the records about request number {index} from the ward office.",
        "source": "consented example",
        "consent": True,
        "labels": {"A": a, "B": b},
    }
    if adjudicated is not None:
        item["adjudicated"] = adjudicated
    return item


def agreed_rows(count: int = 120) -> list[dict]:
    rows = []
    for index in range(1, count + 1):
        positive = index <= count // 2
        # Two deliberate disagreements need adjudication.
        other = positive if index > 2 else not positive
        rows.append(row(index, positive, other, adjudicated=positive if index <= 2 else None))
    return rows


def test_cohen_kappa_is_chance_corrected():
    assert metrics.cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    assert metrics.cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert metrics.cohen_kappa([], []) == 0.0


def test_validation_rejects_missing_consent_and_duplicate_ids():
    rows = agreed_rows(3)
    rows[1]["consent"] = False
    rows[2]["id"] = rows[0]["id"]
    problems = annotate.validate(rows)
    assert any("consent" in problem for problem in problems)
    assert any("duplicate id" in problem for problem in problems)


def test_agreement_reports_kappa_and_pair_count():
    result = annotate.agreement(agreed_rows())
    assert result["evaluated"] is True
    assert result["pairs"] == 120
    assert result["cohen_kappa"] > 0.9
    low, high = result["kappa_ci95"]
    assert low <= result["cohen_kappa"] <= high
    assert set(result["positive_rate"]) == {"A", "B"}


def test_freeze_writes_a_hash_that_verifies(tmp_path):
    frozen = tmp_path / "citizen-requests.jsonl"
    manifest = tmp_path / "citizen-requests.freeze.json"
    written = annotate.freeze(agreed_rows(), frozen_path=frozen, manifest_path=manifest)

    rows, read_manifest, reason = annotate.verify_frozen(frozen, manifest)
    assert reason == ""
    assert rows and len(rows) == 120
    assert read_manifest["sha256"] == written["sha256"]
    assert read_manifest["cohen_kappa"] > 0.9
    assert rows[0]["split"] == "eval"


def test_freeze_refuses_low_agreement(tmp_path):
    rows = [row(i, True, False, adjudicated=True) for i in range(1, 121)]
    with pytest.raises(ValueError, match="kappa"):
        annotate.freeze(
            rows,
            frozen_path=tmp_path / "f.jsonl",
            manifest_path=tmp_path / "f.manifest.json",
        )


def test_freeze_refuses_an_unadjudicated_disagreement(tmp_path):
    rows = agreed_rows()
    rows[10] = row(11, True, False)  # no adjudicated value
    with pytest.raises(ValueError, match="unresolved"):
        annotate.freeze(
            rows,
            frozen_path=tmp_path / "f.jsonl",
            manifest_path=tmp_path / "f.manifest.json",
        )


def test_freeze_refuses_too_few_rows(tmp_path):
    with pytest.raises(ValueError, match="at least 100"):
        annotate.freeze(
            agreed_rows(80),
            frozen_path=tmp_path / "f.jsonl",
            manifest_path=tmp_path / "f.manifest.json",
        )


def test_freeze_refuses_a_one_sided_set(tmp_path):
    rows = [row(i, True, True) for i in range(1, 121)]
    with pytest.raises(ValueError, match="positives and 25 negatives"):
        annotate.freeze(
            rows,
            frozen_path=tmp_path / "f.jsonl",
            manifest_path=tmp_path / "f.manifest.json",
        )


def test_a_tampered_file_is_refused(tmp_path):
    frozen = tmp_path / "citizen-requests.jsonl"
    manifest = tmp_path / "citizen-requests.freeze.json"
    annotate.freeze(agreed_rows(), frozen_path=frozen, manifest_path=manifest)

    with frozen.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "CIT-9999", "text": "added later", "personal_data": True}) + "\n")

    rows, _, reason = annotate.verify_frozen(frozen, manifest)
    assert rows is None
    assert "changed after the manifest" in reason


def test_a_file_without_a_manifest_is_not_scoreable(tmp_path):
    frozen = tmp_path / "citizen-requests.jsonl"
    frozen.write_text('{"id": "CIT-1", "text": "x", "personal_data": true}\n', encoding="utf-8")
    rows, _, reason = annotate.verify_frozen(frozen, tmp_path / "missing.json")
    assert rows is None
    assert "manifest" in reason


def test_the_repository_ships_with_no_citizen_claims():
    rows, manifest, reason = annotate.verify_frozen()
    if rows is None:
        assert reason
        assert manifest is None or manifest.get("cohen_kappa", 0) >= annotate.MIN_KAPPA


def test_redaction_removes_direct_identifiers():
    text, count = annotate.redact(
        "Call me on 9876543210 or me@example.com; my Aadhaar is 1234 5678 9012 "
        "and PAN ABCDE1234F. The request is about ward 12 road spending."
    )
    assert count == 4
    assert "9876543210" not in text
    assert "me@example.com" not in text
    assert "1234 5678 9012" not in text
    assert "ABCDE1234F" not in text
    assert "ward 12" in text


def test_candidates_need_consent_and_are_deduplicated():
    rows: list[dict] = []
    accepted, reason = annotate.add_candidate(
        rows, text="How much was spent on road repairs in ward 12 during 2025-26?",
        source="consented submission", consent=True,
    )
    assert accepted is not None and reason == ""
    assert accepted["id"] == "CIT-0001"
    duplicate, duplicate_reason = annotate.add_candidate(
        rows, text="How much was spent on road repairs in ward 12 during 2025-26?",
        source="another submission", consent=True,
    )
    assert duplicate is None and "duplicate" in duplicate_reason
    refused, refused_reason = annotate.add_candidate(
        rows, text="Please share the officer's home address and phone number tonight.",
        source="x", consent=False,
    )
    assert refused is None and "consent" in refused_reason


def test_a_disagreement_can_only_be_adjudicated_once():
    rows = [row(1, True, False)]
    assert annotate.set_adjudication(rows, "CIT-0001", "yes", "C") is True
    assert rows[0]["adjudicated"] is True
    assert rows[0]["adjudicator"] == "C"
    with pytest.raises(ValueError, match="already adjudicated"):
        annotate.set_adjudication(rows, "CIT-0001", "no", "C")


def test_adjudication_needs_two_labels():
    rows = [{"id": "CIT-1", "text": "x" * 30, "labels": {"A": True}}]
    with pytest.raises(ValueError, match="both annotators"):
        annotate.set_adjudication(rows, "CIT-1", "yes", "C")


def test_progress_counts_the_path_to_one_hundred():
    rows = agreed_rows(120)
    state = annotate.progress(rows, 100)
    assert state["resolved"] == 120
    assert state["complete"] is True
    assert state["remaining_to_target"] == 0
    assert state["positives"] + state["negatives"] == 120
    assert state["disagreements_open"] == 0
