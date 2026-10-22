"""Check where the application fee actually comes from.

The rules file quotes the central fee from the 2012 Fee Rules, but state fees
under sections 27 and 28 vary and were never checked against a gazette copy. The
agent still charges a number for four entries. This check makes that visible: an
entry that claims verification must point at a verified statutory section, an
unverified entry must say why and carry a dated source, and the report lists the
jurisdictions that have no entry at all.

Structural problems fail the build. "Unverified" on its own does not: that is the
current, honestly recorded state. `--strict` is the eventual target (every
jurisdiction with a verified fee) and is expected to fail today.

Writes artifacts/fee_provenance_results.json.

Usage:
    python -m scripts.check_fees
    python -m scripts.check_fees --strict
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common import config

OUT = Path("artifacts/fee_provenance_results.json")
MIN_QUOTE = 30
CENTRAL = "Central"


def load(rules_path: Path | None = None, registry_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    rules_file = rules_path or config.PROJECT_ROOT / "rules" / "rti_rules.json"
    registry_file = registry_path or config.PROJECT_ROOT / "rules" / "authority_registry.json"
    rules_data = json.loads(Path(rules_file).read_text(encoding="utf-8"))
    registry_data = json.loads(Path(registry_file).read_text(encoding="utf-8"))
    return rules_data, registry_data


def _is_amount(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _retrieved_on(value: Any) -> date | None:
    """Accept a date or a timestamp; both appear in the rules and registry."""
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _citation_problems(owner: str, citation: dict[str, Any], *, verified: bool) -> list[str]:
    problems: list[str] = []
    url = urlparse(str(citation.get("url", "")))
    if url.scheme != "https" or not url.netloc:
        problems.append(f"{owner}: citation URL is not an absolute https URL")
    if len(str(citation.get("quote", ""))) <= MIN_QUOTE:
        problems.append(f"{owner}: citation quote is missing or too short")
    retrieved = _retrieved_on(citation.get("retrieved_at"))
    if retrieved is None:
        problems.append(f"{owner}: citation retrieved_at is not an ISO date")
    elif retrieved > date.today():
        problems.append(f"{owner}: citation retrieved_at is in the future")
    if citation.get("verified") is True and not verified:
        problems.append(f"{owner}: citation claims verification for an unverified entry")
    return problems


def _schedule_problems(owner: str, schedule: Any) -> list[str]:
    if not isinstance(schedule, list) or not schedule:
        return [f"{owner}: fee_schedule must be a non-empty list"]
    problems: list[str] = []
    levels: set[str] = set()
    for index, row in enumerate(schedule):
        if not isinstance(row, dict):
            problems.append(f"{owner}: fee_schedule[{index}] is not an object")
            continue
        level = str(row.get("level", "")).strip()
        if not level:
            problems.append(f"{owner}: fee_schedule[{index}] has no level")
        elif level in levels:
            problems.append(f"{owner}: duplicate fee_schedule level {level}")
        levels.add(level)
        if not _is_amount(row.get("fee_inr")):
            problems.append(f"{owner}: fee_schedule[{index}] fee_inr is not a non-negative integer")
    return problems


def _entry_problems(state: str, block: dict[str, Any], sections: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    verified = block.get("verified")
    seeded = block.get("seeded")

    if not isinstance(verified, bool):
        problems.append(f"{state}: verified must be a boolean")
    if not isinstance(seeded, bool):
        problems.append(f"{state}: seeded must be a boolean")
    # The central entry inherits the statutory BPL exemption; a state entry must
    # say explicitly whether it exempts BPL applicants.
    if state != CENTRAL and not isinstance(block.get("bpl_exempt"), bool):
        problems.append(f"{state}: bpl_exempt must be a boolean")

    schedule = block.get("fee_schedule")
    if schedule is not None:
        problems.extend(_schedule_problems(state, schedule))
    if not _is_amount(block.get("fee_inr")) and schedule is None:
        problems.append(f"{state}: fee_inr is not a non-negative integer and no schedule is recorded")

    if verified is True:
        ref = str(block.get("citation_section", "")).strip()
        section = sections.get(ref)
        if not ref:
            problems.append(f"{state}: claims verified without a citation_section")
        elif not isinstance(section, dict):
            problems.append(f"{state}: citation_section {ref} does not exist in sections")
        elif not any(c.get("verified") is True for c in section.get("citations") or []):
            problems.append(f"{state}: citation_section {ref} has no verified citation")
        else:
            for citation in section.get("citations") or []:
                problems.extend(_citation_problems(f"{state} ({ref})", citation, verified=True))
    else:
        if not str(block.get("why_unverified", "")).strip():
            problems.append(f"{state}: unverified but no reason recorded")

    citations = block.get("citations")
    if citations is not None and not isinstance(citations, list):
        problems.append(f"{state}: citations must be a list")
        citations = []
    citations = citations or []
    for citation in citations:
        if not isinstance(citation, dict):
            problems.append(f"{state}: citation is not an object")
            continue
        problems.extend(_citation_problems(state, citation, verified=verified is True))
    # A verified entry may cite a statutory section instead of carrying its own
    # citations (the central fee does this). An unverified entry has nothing but
    # its own dated sources, so it must carry at least one.
    if verified is not True and not citations:
        problems.append(f"{state}: seeded fee cites no source")
    return problems


def audit(rules_data: dict[str, Any], registry_data: dict[str, Any]) -> dict[str, Any]:
    """Return a coverage report and every structural problem. Pure, no I/O."""
    sections = rules_data.get("sections") or {}
    entries = rules_data.get("state_assumptions") or {}
    jurisdictions = set(registry_data.get("jurisdictions") or [])

    problems: list[str] = []
    per_entry: dict[str, Any] = {}
    for state, block in entries.items():
        if not isinstance(block, dict):
            problems.append(f"{state}: fee entry is not an object")
            continue
        entry_problems = _entry_problems(state, block, sections)
        problems.extend(entry_problems)
        per_entry[state] = {
            "verified": block.get("verified") is True,
            "seeded": block.get("seeded") is True,
            "fee_inr": block.get("fee_inr"),
            "tiered": bool(block.get("fee_schedule")),
            "citation_section": block.get("citation_section"),
            "citations": len(block.get("citations") or []),
            "problems": entry_problems,
        }

    unknown = sorted(name for name in entries if name != CENTRAL and name not in jurisdictions)
    for name in unknown:
        problems.append(f"{name}: fee entry for an unknown jurisdiction")

    with_entry = sorted(jurisdictions & set(entries))
    missing = sorted(jurisdictions - set(entries))
    verified_entries = sorted(
        name
        for name in with_entry
        if isinstance(entries.get(name), dict) and entries[name].get("verified") is True
    )
    central = entries.get(CENTRAL) if isinstance(entries.get(CENTRAL), dict) else {}
    central_verified = bool(
        central.get("verified") is True
        and not any(p.startswith(CENTRAL) for p in per_entry.get(CENTRAL, {}).get("problems", []))
    )

    return {
        "rules_version": rules_data.get("version"),
        "registry_version": registry_data.get("version"),
        "coverage": {
            "jurisdictions": len(jurisdictions),
            "with_fee_entry": with_entry,
            "missing_fee_entry": missing,
            "verified_fee_entries": verified_entries,
            "central_verified": central_verified,
            "state_entries": len(entries),
        },
        "entries": per_entry,
        "problems": problems,
        "gate": {
            "structural_problems": len(problems),
            "missing_fee_entries": len(missing),
            "verified_fee_entries": len(verified_entries),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit state RTI fee provenance and coverage.")
    parser.add_argument("--rules", type=Path, default=config.PROJECT_ROOT / "rules" / "rti_rules.json")
    parser.add_argument(
        "--registry", type=Path, default=config.PROJECT_ROOT / "rules" / "authority_registry.json"
    )
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when any jurisdiction lacks a verified fee (the target, not today's state)",
    )
    args = parser.parse_args(argv)

    rules_data, registry_data = load(args.rules, args.registry)
    payload = audit(rules_data, registry_data)
    payload["strict"] = bool(args.strict)
    # `jurisdictions` is the total count; verified_fee_entries is the list. Strict
    # means every jurisdiction must end up with a verified fee.
    payload["gate"]["strict_failures"] = payload["coverage"]["jurisdictions"] - len(
        payload["coverage"]["verified_fee_entries"]
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    coverage = payload["coverage"]
    verified = len(coverage["verified_fee_entries"])
    print(
        f"fees: {len(coverage['with_fee_entry'])}/{coverage['jurisdictions']} jurisdictions have a fee "
        f"entry, {verified} state entries verified, central rule "
        f"{'verified' if coverage['central_verified'] else 'unverified'}"
    )
    for problem in payload["problems"]:
        print(f"  PROBLEM: {problem}")
    if args.strict and payload["gate"]["strict_failures"]:
        print(
            f"  STRICT: {payload['gate']['strict_failures']} jurisdiction(s) still lack a verified "
            "fee entry; this is the target, not the current state"
        )
    print(f"Wrote {args.out}")
    if payload["problems"]:
        return 1
    return 1 if args.strict and payload["gate"]["strict_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
