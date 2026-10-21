"""Parsing helpers for the Source B benchmark."""

from __future__ import annotations

from scripts import bench_source_b


def test_sections_of_accepts_csv_string() -> None:
    assert bench_source_b.sections_of("['8(1)(j)', '8(1)(h)']") == ["8(1)(j)", "8(1)(h)"]
    assert bench_source_b.sections_of("[]") == []


def test_sections_of_accepts_jsonl_list() -> None:
    assert bench_source_b.sections_of(["8(1)(e)"]) == ["8(1)(e)"]
    assert bench_source_b.sections_of(()) == []


def test_sections_of_survives_malformed_input() -> None:
    assert bench_source_b.sections_of("not a list") == []
    assert bench_source_b.sections_of("") == []


def test_request_span_finds_and_returns_none() -> None:
    text = "The appellant sought information regarding the road repair bills for 2024."
    span = bench_source_b.request_span(text)
    assert span is not None and "road repair bills" in span
    assert bench_source_b.request_span("The Commission dismissed the appeal.") is None
