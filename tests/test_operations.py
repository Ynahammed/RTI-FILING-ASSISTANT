"""Metric maths for scripts/bench_operations.py, exercised without a running system."""

from __future__ import annotations

from scripts import bench_operations as ops


def test_percentile_handles_empty_and_small_samples() -> None:
    assert ops.percentile([], 50) == 0.0
    assert ops.percentile([10.0], 95) == 10.0
    assert ops.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert ops.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == 5.0


def _record(status: str, **extra):
    base = {"status": status, "blocked": False, "actions": [], "total_ms": 100.0, "expected_exempt": False}
    base.update(extra)
    return base


def test_summary_counts_outcomes_and_integrity() -> None:
    records = [
        _record("verified", reg_no="MOCK/A/1"),
        _record("verified", reg_no="MOCK/A/2"),
        _record("review_queue", blocked=True, expected_exempt=True),
        _record("redirected"),
        _record("filed"),
    ]
    summary = ops.summarise(records, concurrency=2, wall_seconds=60.0)
    assert summary["attempted"] == 5
    assert summary["verified"] == 2
    assert summary["blocked_by_exemption"] == 1
    assert summary["redirected_to_grievance"] == 1
    # filed is a live status, not a terminal one
    assert summary["completion_rate"] == 0.8
    assert summary["duplicate_registrations"] == 0
    assert summary["throughput_runs_per_min"] == 5.0


def test_duplicate_registrations_are_reported() -> None:
    records = [_record("verified", reg_no="MOCK/A/1"), _record("verified", reg_no="MOCK/A/1")]
    summary = ops.summarise(records, concurrency=1, wall_seconds=10.0)
    assert summary["duplicate_registrations"] == 1
    assert summary["gate"]["duplicate_registrations"] == 1


def test_exemption_pairs_only_use_runs_that_finished() -> None:
    records = [
        _record("verified", expected_exempt=False),
        _record("review_queue", blocked=True, expected_exempt=True),
        {"status": "error", "error": "boom", "expected_exempt": True},
    ]
    summary = ops.summarise(records, concurrency=1, wall_seconds=10.0)
    screen = summary["exemption_screen_vs_dataset"]
    assert (screen["tp"], screen["fp"], screen["fn"]) == (1, 0, 0)
    assert summary["errors"] == 1
