"""Check that every rule the product acts on carries a source.

The rules file used to call itself "seeded assumptions" in one sentence and then
drive a 30-day deadline, a 48-hour clock, a five-day transfer and a fee. This check
makes that visible: a section without a citation, or a state fee that claims to be
verified without one, fails the build.

Writes artifacts/rules_provenance_results.json. Exit code is non-zero on a problem.

Usage:
    python -m scripts.check_rules
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import rules

OUT = Path("artifacts/rules_provenance_results.json")


def main() -> int:
    provenance = rules.provenance()
    payload = {
        "rules_version": provenance["version"],
        "provenance": provenance,
        "gate": {
            "uncited": provenance["uncited"],
            "unverified_claims": len(provenance["problems"]),
            "sections_cited": provenance["sections_cited"],
            "sections_required": provenance["sections_required"],
            "verified_states": provenance["states_verified"],
            "seeded_states": provenance["states_seeded"],
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"rules v{provenance['version']}: {provenance['sections_cited']}/"
        f"{provenance['sections_required']} sections cited, "
        f"{provenance['states_verified']} state entries verified, "
        f"{provenance['states_seeded']} seeded"
    )
    if provenance["problems"]:
        for problem in provenance["problems"]:
            print(f"  PROBLEM: {problem}")
        print(f"Wrote {OUT}")
        return 1
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
