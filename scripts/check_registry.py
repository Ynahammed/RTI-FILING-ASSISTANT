"""Measure how much of the country the authority registry can actually route to.

The registry used to cover four jurisdictions, so any request outside them fell to
"no entry" and stopped. It now covers every state and union territory, but only four
with hand-shaped entries; the rest are explicit generic fallbacks that name no office
and cannot be routed without the citizen confirming them.

This check makes that distinction visible and fails on an uncovered jurisdiction, a
duplicate code, or an entry missing the provenance the evidence ledger needs.

Writes artifacts/registry_coverage_results.json.

Usage:
    python -m scripts.check_registry
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from common import config

OUT = Path("artifacts/registry_coverage_results.json")
REQUIRED_FIELDS = (
    "code", "level", "state", "department", "name", "pio", "source_id", "source_url", "retrieved_at",
)


def load() -> dict:
    return json.loads(
        (config.PROJECT_ROOT / "rules" / "authority_registry.json").read_text(encoding="utf-8")
    )


def main() -> int:
    data = load()
    authorities = data["authorities"]
    jurisdictions = set(data.get("jurisdictions") or [])
    covered = {a["state"] for a in authorities}
    uncovered = sorted(jurisdictions - covered)

    codes = Counter(a["code"] for a in authorities)
    duplicates = sorted(code for code, count in codes.items() if count > 1)

    missing = []
    for authority in authorities:
        for field in REQUIRED_FIELDS:
            if not str(authority.get(field) or "").strip():
                missing.append(f"{authority.get('code', '?')}: missing {field}")
        url = urlparse(str(authority.get("source_url", "")))
        if url.scheme not in {"http", "https"} or not url.netloc:
            missing.append(f"{authority.get('code', '?')}: source_url is not absolute")
        if authority.get("generic") and authority.get("verified") is True:
            missing.append(f"{authority.get('code', '?')}: generic entry claims to be verified")
        if not authority.get("generic") and authority.get("verified") is False:
            missing.append(f"{authority.get('code', '?')}: named entry is marked unverified")

    generic = [a for a in authorities if a.get("generic")]
    verified_states = {a["state"] for a in authorities if not a.get("generic")}
    payload = {
        "registry_version": data.get("version"),
        "coverage": {
            "jurisdictions": len(jurisdictions),
            "covered": len(jurisdictions & covered),
            "uncovered": uncovered,
            "verified_jurisdictions": sorted(verified_states),
            "generic_jurisdictions": len({a["state"] for a in generic}),
            "entries": len(authorities),
            "verified_entries": len(authorities) - len(generic),
            "generic_entries": len(generic),
        },
        "problems": missing + [f"duplicate code {code}" for code in duplicates],
        "gate": {
            "uncovered": len(uncovered),
            "missing_provenance": len(missing),
            "duplicate_codes": len(duplicates),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    c = payload["coverage"]
    print(
        f"registry: {c['covered']}/{c['jurisdictions']} jurisdictions, "
        f"{c['verified_entries']} verified entries, {c['generic_entries']} generic"
    )
    for problem in payload["problems"]:
        print(f"  PROBLEM: {problem}")
    print(f"Wrote {OUT}")
    return 0 if not payload["problems"] and not uncovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
