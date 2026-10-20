"""Generate the two shared secrets a deployment needs.

Writes `deploy/.secrets.env` (gitignored) with fresh values and never prints
them. Both services must receive the same RTI_APPROVAL_SECRET, or the portal
will reject every approval token.

    python -m scripts.gen_secrets            # create if missing
    python -m scripts.gen_secrets --force    # rotate
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / ".secrets.env"

KEYS = ("RTI_APPROVAL_SECRET", "RTI_ADMIN_TOKEN")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rotate even if the file exists")
    args = parser.parse_args()

    if OUT.exists() and not args.force:
        existing = dict(
            line.split("=", 1) for line in OUT.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        missing = [k for k in KEYS if not existing.get(k)]
        if not missing:
            print(f"{OUT.relative_to(ROOT)} already has both secrets; use --force to rotate.")
            return 0

    values = {key: secrets.token_urlsafe(48) for key in KEYS}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    try:
        OUT.chmod(0o600)
    except OSError:
        pass
    print(f"Wrote {OUT.relative_to(ROOT)} with {len(values)} secrets (values not shown).")
    print("Set the SAME RTI_APPROVAL_SECRET on both services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
