"""Access to the seeded RTI rules file."""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from common import config

# Every rule the product acts on. A rule without a citation is a claim, not law.
REQUIRED_SECTIONS = (
    "6(3)", "7(1)", "8(1)(j)", "19", "19(3)", "19(6)", "18(1)", "20(1)", "fee",
)
MIN_QUOTE = 30


@lru_cache(maxsize=1)
def rules() -> dict[str, Any]:
    return json.loads(config.RULES_PATH.read_text(encoding="utf-8"))


def section(ref: str) -> dict[str, Any]:
    return rules()["sections"].get(ref, {})


def fee_default() -> int:
    return int(section("fee").get("default_inr", 10))


def bpl_exempt() -> bool:
    return bool(section("fee").get("bpl_exempt", True))


def routing_threshold() -> float:
    return float(rules()["routing"].get("confidence_threshold", 0.7))


def review_threshold() -> float:
    """Classifier score at or above which a request goes to human review.

    Set from measured out-of-fold performance, not intuition: see
    scripts/bench_review_routing.py and artifacts/review_routing_results.json.
    """
    return float(rules().get("exemption_review", {}).get("threshold", 0.42))


def rule_summary() -> str:
    return "RTI rules v" + str(rules().get("version", "?")) + " — " + str(rules().get("disclaimer", ""))


def _citation_problems(ref: str, citation: dict[str, Any], *, require_verified: bool = True) -> list[str]:
    problems: list[str] = []
    url = str(citation.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        problems.append(f"{ref}: citation URL is not an absolute http(s) URL")
    if len(str(citation.get("quote", ""))) < MIN_QUOTE:
        problems.append(f"{ref}: citation quote is missing or too short")
    try:
        date.fromisoformat(str(citation.get("retrieved_at", "")))
    except ValueError:
        problems.append(f"{ref}: citation needs an ISO retrieval date")
    if require_verified and citation.get("verified") is not True:
        problems.append(f"{ref}: citation is not marked verified")
    return problems


def validate(data: dict[str, Any] | None = None) -> list[str]:
    """Return every provenance problem. Empty means every rule is cited."""
    data = data or rules()
    sections = data.get("sections", {})
    problems: list[str] = []
    for ref in REQUIRED_SECTIONS:
        section = sections.get(ref)
        if not section:
            problems.append(f"missing required section {ref}")
            continue
        citations = section.get("citations") or []
        if not citations:
            problems.append(f"{ref}: no citation")
            continue
        for citation in citations:
            problems.extend(_citation_problems(ref, citation))

    for state, block in (data.get("state_assumptions") or {}).items():
        # A state entry may carry a citation without claiming it is verified: that is
        # exactly the "reported but not gazette-checked" state, and it must still have
        # a real URL, quote and retrieval date.
        for citation in block.get("citations") or []:
            problems.extend(_citation_problems(state, citation, require_verified=False))
        if block.get("verified") is True:
            if block.get("citation_section"):
                target = sections.get(block["citation_section"], {})
                if not target.get("citations"):
                    problems.append(f"{state}: points at an uncited section")
            elif not any(c.get("verified") is True for c in block.get("citations") or []):
                problems.append(f"{state}: claims verified without a citation")
            continue
        if not block.get("seeded"):
            problems.append(f"{state}: neither verified nor marked seeded")
        elif not str(block.get("why_unverified", "")).strip():
            problems.append(f"{state}: seeded but no reason recorded")
        elif not block.get("citations"):
            problems.append(f"{state}: seeded without a source for the reported value")
    return problems


def provenance() -> dict[str, Any]:
    """Coverage summary for the API and the provenance gate."""
    data = rules()
    sections = data.get("sections", {})
    cited = sum(1 for ref in REQUIRED_SECTIONS if sections.get(ref, {}).get("citations"))
    verified = sum(
        1
        for ref in REQUIRED_SECTIONS
        if any(c.get("verified") is True for c in sections.get(ref, {}).get("citations", []))
    )
    states = data.get("state_assumptions") or {}
    return {
        "version": data.get("version"),
        "sections_required": len(REQUIRED_SECTIONS),
        "sections_cited": cited,
        "sections_verified": verified,
        "uncited": len(REQUIRED_SECTIONS) - cited,
        "state_entries": len(states),
        "states_verified": sum(1 for block in states.values() if block.get("verified") is True),
        "states_cited": sum(1 for block in states.values() if block.get("citations")),
        "states_seeded": sum(1 for block in states.values() if block.get("seeded") is True),
        "problems": validate(data),
    }


def state_fee(state: str) -> dict[str, Any]:
    """Fee data for a state, or the central rule when the state has no entry."""
    block = dict((rules().get("state_assumptions") or {}).get(state) or {})
    if not block:
        block = {"fee_inr": fee_default(), "bpl_exempt": bpl_exempt(), "source": "fee"}
    block["schedule"] = block.get("fee_schedule") or []
    return block
