"""Annotation pipeline for citizen-phrased RTI requests.

Every other benchmark in this repository scores text from appellate decisions.
That is not the text a citizen writes, so no number here can honestly claim
citizen-request accuracy. This pipeline is how that data gets collected, labelled
twice, adjudicated and frozen — and it refuses to produce a benchmark file that
is not defensible:

* two independent annotators per request, with Cohen's kappa and a bootstrap CI;
* a target and a floor of 100 adjudicated rows, with at least 25 positives and
  25 negatives so neither class is a rounding error;
* a frozen file plus a manifest recording its SHA-256, so a benchmark can prove
  it scored the same rows that were reviewed.

It deliberately ships with no data. `stats` shows the gap and how far the target
is; `python -m scripts.annotate_server` is the labelling UI for non-engineers.

Storage
-------
`data/citizen-annotations.jsonl` — one object per request:

    {"id": "CIT-0001", "text": "...", "source": "consented example",
     "consent": true, "language": "en",
     "labels": {"A": true, "B": false},
     "adjudicated": true, "adjudicator": "C",
     "redactions": 2, "text_sha256": "..."}

Commands
--------
    python -m scripts.annotate_citizen stats
    python -m scripts.annotate_citizen import --input candidates.jsonl
    python -m scripts.annotate_citizen label --annotator A --labels labels.jsonl
    python -m scripts.annotate_citizen review
    python -m scripts.annotate_citizen adjudicate --id CIT-0007 --value yes --adjudicator C
    python -m scripts.annotate_citizen freeze

`candidates.jsonl`: {"text": "...", "source": "...", "consent": true, "language": "en"}
`labels.jsonl`:      {"id": "CIT-0001", "personal_data": true}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from common import metrics

ROOT = Path(__file__).resolve().parent.parent
ANNOTATIONS = ROOT / "data" / "citizen-annotations.jsonl"
FROZEN = ROOT / "data" / "citizen-requests.jsonl"
MANIFEST = ROOT / "data" / "citizen-requests.freeze.json"
MIN_KAPPA = 0.6
TARGET_ROWS = 100
MIN_ROWS = 100
MIN_POSITIVES = 25
MIN_NEGATIVES = 25
MIN_TEXT = 20
ANNOTATOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# The one question both annotators answer. Kept here so the CLI, the web UI and
# the codebook cannot drift apart.
LABEL_QUESTION = (
    "Does this request seek another person's private information — their address, "
    "phone number, identity, health, family or financial details — rather than "
    "information about how a public body works or spends money?"
)

_STORE_LOCK = threading.Lock()

# Conservative identifier patterns. They are applied on import, before any
# annotator sees the text, and the count is stored so the redaction is auditable.
_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[-\s]?)?[6-9]\d{9}(?!\d)")),
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )


def redact(text: str) -> tuple[str, int]:
    """Remove direct identifiers before anyone labels the text.

    Names and addresses cannot be found reliably by pattern, so the consent form
    asks contributors not to include them; this pass catches the identifiers that
    can be removed mechanically, and the count is recorded on the row.
    """
    count = 0
    for label, pattern in _REDACTIONS:
        text, found = pattern.subn(f"[{label}]", text)
        count += found
    return text, count


def text_digest(text: str) -> str:
    normalised = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def validate(rows: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        tag = row.get("id") or f"line {index}"
        if not row.get("id"):
            problems.append(f"{tag}: missing id")
        elif row["id"] in seen:
            problems.append(f"{tag}: duplicate id")
        else:
            seen.add(row["id"])
        if len(str(row.get("text", "")).strip()) < MIN_TEXT:
            problems.append(f"{tag}: text is shorter than {MIN_TEXT} characters")
        if row.get("consent") is not True:
            problems.append(f"{tag}: consent is not recorded as true")
        if not str(row.get("source", "")).strip():
            problems.append(f"{tag}: source is missing")
        for annotator, value in (row.get("labels") or {}).items():
            if not ANNOTATOR_RE.match(str(annotator)):
                problems.append(f"{tag}: annotator name {annotator!r} is not a safe identifier")
            if not isinstance(value, bool):
                problems.append(f"{tag}: label from {annotator} is not a boolean")
        if "adjudicated" in row and not isinstance(row["adjudicated"], bool):
            problems.append(f"{tag}: adjudicated is not a boolean")
    return problems


def annotators(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({name for row in rows for name in (row.get("labels") or {})})


def pairable(rows: list[dict[str, Any]], first: str, second: str) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if isinstance((row.get("labels") or {}).get(first), bool)
        and isinstance((row.get("labels") or {}).get(second), bool)
    ]


def _paired(rows: list[dict[str, Any]]) -> tuple[str, str, list[dict[str, Any]]] | None:
    names = annotators(rows)
    if len(names) < 2:
        return None
    first, second = names[0], names[1]
    pairs = pairable(rows, first, second)
    if not pairs:
        return None
    return first, second, pairs


def agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = _paired(rows)
    if paired is None:
        return {
            "evaluated": False,
            "reason": "fewer than two annotators have labelled the same rows",
            "annotators": annotators(rows),
            "pairs": 0,
        }
    first, second, pairs = paired
    a = [bool(row["labels"][first]) for row in pairs]
    b = [bool(row["labels"][second]) for row in pairs]
    kappa = metrics.cohen_kappa(a, b)
    raw = sum(x == y for x, y in zip(a, b)) / len(pairs)
    # Paired rows are the resampling unit: labels inside a row are dependent.
    ci_low, ci_high = metrics.bootstrap_interval(
        list(zip(a, b)),
        lambda sample: metrics.cohen_kappa([x for x, _ in sample], [y for _, y in sample]),
    )
    return {
        "evaluated": True,
        "annotators": [first, second],
        "pairs": len(pairs),
        "cohen_kappa": kappa,
        "kappa_ci95": [ci_low, ci_high],
        "raw_agreement": round(raw, 4),
        "threshold": MIN_KAPPA,
        "positive_rate": {
            first: round(sum(a) / len(a), 4),
            second: round(sum(b) / len(b), 4),
        },
    }


def disputed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paired = _paired(rows)
    if paired is None:
        return []
    first, second, pairs = paired
    return [
        row for row in pairs
        if bool(row["labels"][first]) != bool(row["labels"][second])
        and not isinstance(row.get("adjudicated"), bool)
    ]


def unresolved(rows: list[dict[str, Any]]) -> list[str]:
    paired = _paired(rows)
    if paired is None:
        return [row.get("id", "?") for row in rows]
    first, second, pairs = paired
    paired_ids = {row.get("id") for row in pairs}
    missing = [row.get("id", "?") for row in rows if row.get("id") not in paired_ids]
    missing.extend(row.get("id", "?") for row in disputed(rows))
    return missing


def adjudicated_label(row: dict[str, Any]) -> bool:
    labels = row.get("labels") or {}
    if isinstance(row.get("adjudicated"), bool):
        return bool(row["adjudicated"])
    values = [bool(value) for value in labels.values() if isinstance(value, bool)]
    if values and len(set(values)) == 1:
        return values[0]
    raise ValueError(f"{row.get('id')}: no adjudicated label")


def progress(rows: list[dict[str, Any]], target: int = TARGET_ROWS) -> dict[str, Any]:
    """How far the set is from a frozen, benchmarkable 100 rows."""
    names = annotators(rows)
    paired = _paired(rows)
    pairs = len(paired[2]) if paired else 0
    disputed_rows = disputed(rows)
    resolved = sum(1 for row in rows if _is_resolved(row))
    return {
        "candidates": len(rows),
        "annotators": names,
        "labelled_by_annotator": {
            name: sum(1 for row in rows if isinstance((row.get("labels") or {}).get(name), bool))
            for name in names
        },
        "pairs": pairs,
        "disagreements": len(disputed_rows) + sum(
            1 for row in rows
            if isinstance(row.get("adjudicated"), bool)
            and paired is not None
            and bool(row["labels"][paired[0]]) != bool(row["labels"][paired[1]])
        ),
        "disagreements_open": len(disputed_rows),
        "adjudicated": sum(1 for row in rows if isinstance(row.get("adjudicated"), bool)),
        "resolved": resolved,
        "target": target,
        "remaining_to_target": max(0, target - resolved),
        "positives": sum(1 for row in rows if _is_resolved(row) and adjudicated_label(row)),
        "negatives": sum(1 for row in rows if _is_resolved(row) and not adjudicated_label(row)),
        "complete": resolved >= target,
    }


def _is_resolved(row: dict[str, Any]) -> bool:
    """Resolved means both annotators agree, or a dispute has been adjudicated.

    One label is not a resolved row, which is why the status page counts pairs rather
    than every label written.
    """
    if isinstance(row.get("adjudicated"), bool):
        return True
    labels = [value for value in (row.get("labels") or {}).values() if isinstance(value, bool)]
    return len(labels) >= 2 and len(set(labels)) == 1


def normalize_label(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"yes", "y", "true", "1", "personal", "personal_data"}:
            return True
        if text in {"no", "n", "false", "0", "not_personal", "otherwise"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def add_candidate(
    rows: list[dict[str, Any]],
    *,
    text: str,
    source: str,
    consent: bool,
    language: str = "en",
) -> tuple[dict[str, Any] | None, str]:
    """Add one consented request, redacted and de-duplicated. Returns (row, reason)."""
    cleaned, redactions = redact(str(text).strip())
    if len(cleaned) < MIN_TEXT:
        return None, f"text is shorter than {MIN_TEXT} characters"
    if not consent:
        return None, "consent is required"
    if not str(source).strip():
        return None, "source is required"
    digest = text_digest(cleaned)
    for row in rows:
        if row.get("text_sha256") == digest or text_digest(str(row.get("text", ""))) == digest:
            return None, "duplicate text already in the set"
    row = {
        "id": f"CIT-{len(rows) + 1:04d}",
        "text": cleaned,
        "source": str(source).strip(),
        "consent": True,
        "language": str(language or "en").strip()[:8],
        "labels": {},
        "redactions": redactions,
        "text_sha256": digest,
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    rows.append(row)
    return row, ""


def apply_label(rows: list[dict[str, Any]], row_id: str, annotator: str, value: Any) -> bool:
    if not ANNOTATOR_RE.match(str(annotator or "")):
        raise ValueError("annotator must be 1-32 letters, digits, dash or underscore")
    label = normalize_label(value)
    if label is None:
        raise ValueError("label must be yes or no")
    for row in rows:
        if row.get("id") == row_id:
            row.setdefault("labels", {})[annotator] = label
            return True
    raise KeyError(row_id)


def set_adjudication(
    rows: list[dict[str, Any]], row_id: str, value: Any, adjudicator: str
) -> bool:
    """Resolve a disagreement. Only a disputed row can be adjudicated."""
    label = normalize_label(value)
    if label is None:
        raise ValueError("label must be yes or no")
    if not ANNOTATOR_RE.match(str(adjudicator or "")):
        raise ValueError("adjudicator must be 1-32 letters, digits, dash or underscore")
    for row in rows:
        if row.get("id") != row_id:
            continue
        if isinstance(row.get("adjudicated"), bool):
            raise ValueError("already adjudicated; the decision is immutable")
        labels = [v for v in (row.get("labels") or {}).values() if isinstance(v, bool)]
        if len(labels) < 2:
            raise ValueError("both annotators must label a row before adjudication")
        if len(set(labels)) == 1:
            raise ValueError("nothing to adjudicate: the annotators already agree")
        row["adjudicated"] = label
        row["adjudicator"] = adjudicator
        row["adjudicated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return True
    raise KeyError(row_id)


def freeze(
    rows: list[dict[str, Any]],
    *,
    frozen_path: Path = FROZEN,
    manifest_path: Path = MANIFEST,
) -> dict[str, Any]:
    """Write the eval split and a manifest, or raise with the reason it is not defensible."""
    problems = validate(rows)
    if problems:
        raise ValueError("annotation problems: " + "; ".join(problems[:5]))
    synthetic = [row.get("id") for row in rows if row.get("synthetic") is True]
    if synthetic:
        raise ValueError(
            "synthetic rows cannot be frozen as evidence; they are for pipeline testing only "
            f"({len(synthetic)} rows, e.g. {synthetic[:3]})"
        )
    if len(rows) < MIN_ROWS:
        raise ValueError(f"need at least {MIN_ROWS} adjudicated rows, have {len(rows)}")
    agreement_result = agreement(rows)
    if not agreement_result["evaluated"]:
        raise ValueError("need two annotators before freezing")
    if agreement_result["cohen_kappa"] < MIN_KAPPA:
        raise ValueError(
            f"Cohen's kappa {agreement_result['cohen_kappa']} is below {MIN_KAPPA}; "
            "annotators must agree on the definition before the split is frozen"
        )
    missing = unresolved(rows)
    if missing:
        raise ValueError(f"{len(missing)} rows are unresolved (disagreement without adjudication): {missing[:5]}")
    labels = [adjudicated_label(row) for row in rows]
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives < MIN_POSITIVES or negatives < MIN_NEGATIVES:
        raise ValueError(
            f"need at least {MIN_POSITIVES} positives and {MIN_NEGATIVES} negatives, "
            f"have {positives} and {negatives}"
        )

    frozen_rows = [
        {
            "id": row["id"],
            "text": row["text"].strip(),
            "personal_data": label,
            "sections": ["8(1)(j)"] if label else [],
            "split": "eval",
            "language": row.get("language", "en"),
        }
        for row, label in zip(rows, labels)
    ]
    write_jsonl(frozen_path, frozen_rows)
    digest = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    manifest = {
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": digest,
        "rows": len(frozen_rows),
        "positives": positives,
        "negatives": negatives,
        "annotators": agreement_result["annotators"],
        "cohen_kappa": agreement_result["cohen_kappa"],
        "kappa_ci95": agreement_result["kappa_ci95"],
        "raw_agreement": agreement_result["raw_agreement"],
        "min_kappa": MIN_KAPPA,
        "min_rows": MIN_ROWS,
        "min_positives": MIN_POSITIVES,
        "min_negatives": MIN_NEGATIVES,
        "source_file": str(frozen_path.name),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify_frozen(
    frozen_path: Path = FROZEN, manifest_path: Path = MANIFEST
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, str]:
    """Return (rows, manifest, reason). reason is empty when the split is usable."""
    if not frozen_path.exists():
        return None, None, "no frozen citizen-request file"
    if not manifest_path.exists():
        return None, None, "frozen file has no manifest; it was not produced by freeze"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    if digest != manifest.get("sha256"):
        return None, manifest, "frozen file changed after the manifest was written"
    if manifest.get("cohen_kappa", 0) < MIN_KAPPA:
        return None, manifest, "annotator agreement is below the threshold"
    if manifest.get("rows", 0) < MIN_ROWS:
        return None, manifest, "fewer rows than the minimum"
    if manifest.get("positives", 0) < MIN_POSITIVES or manifest.get("negatives", 0) < MIN_NEGATIVES:
        return None, manifest, "one class is too small to score"
    return read_jsonl(frozen_path), manifest, ""


# ------------------------------------------------------------------- storage


def load(path: Path | str = ANNOTATIONS) -> list[dict[str, Any]]:
    return read_jsonl(Path(path))


def save(rows: list[dict[str, Any]], path: Path | str = ANNOTATIONS) -> None:
    write_jsonl(Path(path), rows)


def locked_update(path: Path | str, mutate) -> Any:
    """Read-modify-write one JSONL file under a lock, for concurrent annotators."""
    with _STORE_LOCK:
        rows = load(path)
        result = mutate(rows)
        save(rows, path)
        return result


# ---------------------------------------------------------------------- CLI


def _load(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)


def cmd_stats(args: argparse.Namespace) -> int:
    rows = _load(Path(args.annotations))
    state = progress(rows, args.target)
    verdict = agreement(rows)
    print(f"annotations: {len(rows)} candidates in {args.annotations}")
    if not rows:
        print("No citizen-request candidates yet.")
        print("Collect with `python -m scripts.annotate_server` (contributor form) or import a file.")
    print(f"progress: {state['resolved']}/{state['target']} resolved, {state['remaining_to_target']} to go")
    print(f"  pairs={state['pairs']} disputed_open={state['disagreements_open']} adjudicated={state['adjudicated']}")
    print(f"  positives={state['positives']} negatives={state['negatives']} (need >={MIN_POSITIVES}/{MIN_NEGATIVES})")
    print(f"  labelled by annotator: {state['labelled_by_annotator']}")
    print(f"agreement: {verdict}")
    frozen, manifest, reason = verify_frozen(Path(args.frozen), Path(args.manifest))
    print(f"frozen split: {'usable' if frozen is not None else 'not usable: ' + reason}")
    if manifest:
        print(f"  manifest: {manifest.get('rows')} rows, kappa {manifest.get('cohen_kappa')}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    candidates = read_jsonl(Path(args.input))
    path = Path(args.annotations)

    def mutate(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
        added = 0
        skipped: list[str] = []
        for candidate in candidates:
            row, reason = add_candidate(
                rows,
                text=str(candidate.get("text", "")),
                source=str(candidate.get("source", "")),
                consent=candidate.get("consent") is True,
                language=str(candidate.get("language", "en")),
            )
            if row is None:
                skipped.append(reason)
            else:
                added += 1
        return added, skipped

    added, skipped = locked_update(path, mutate)
    print(f"imported {added} candidates; {len(_load(path))} total")
    for reason in skipped[:10]:
        print(f"  skipped: {reason}")
    problems = validate(_load(path))
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return 1 if problems else 0


def cmd_label(args: argparse.Namespace) -> int:
    labels = read_jsonl(Path(args.labels))

    def mutate(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
        applied = 0
        errors: list[str] = []
        for entry in labels:
            try:
                if apply_label(rows, str(entry.get("id")), args.annotator, entry.get("personal_data")):
                    applied += 1
            except (KeyError, ValueError) as exc:
                errors.append(f"{entry.get('id')}: {exc}")
        return applied, errors

    applied, errors = locked_update(Path(args.annotations), mutate)
    print(f"{args.annotator}: applied {applied} labels")
    for error in errors[:10]:
        print(f"  skipped: {error}")
    print(f"agreement: {agreement(_load(Path(args.annotations)))}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    rows = _load(Path(args.annotations))
    open_disputes = disputed(rows)
    print(f"{len(open_disputes)} open disagreements")
    for row in open_disputes[: args.limit]:
        labels = row.get("labels") or {}
        print(f"  {row['id']}  {labels}  {row['text'][:110]}")
    if len(open_disputes) > args.limit:
        print(f"  … and {len(open_disputes) - args.limit} more. Use the adjudication page.")
    return 0


def cmd_adjudicate(args: argparse.Namespace) -> int:
    def mutate(rows: list[dict[str, Any]]) -> None:
        if not set_adjudication(rows, args.id, args.value, args.adjudicator):
            raise KeyError(args.id)

    try:
        locked_update(Path(args.annotations), mutate)
    except KeyError:
        print(f"no candidate with id {args.id}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    print(f"{args.id}: adjudicated as {normalize_label(args.value)} by {args.adjudicator}")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    rows = _load(Path(args.annotations))
    try:
        manifest = freeze(rows, frozen_path=Path(args.frozen), manifest_path=Path(args.manifest))
    except ValueError as exc:
        print(f"refusing to freeze: {exc}", file=sys.stderr)
        return 1
    print(f"froze {manifest['rows']} rows ({manifest['positives']} positive), kappa {manifest['cohen_kappa']}")
    print(f"kappa 95% CI {manifest['kappa_ci95']}")
    print(f"sha256 {manifest['sha256']}")
    print(f"Wrote {args.frozen} and {args.manifest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--annotations", default=os.getenv("RTI_CITIZEN_ANNOTATIONS", str(ANNOTATIONS))
    )
    parser.add_argument("--frozen", default=os.getenv("RTI_CITIZEN_FROZEN", str(FROZEN)))
    parser.add_argument("--manifest", default=os.getenv("RTI_CITIZEN_MANIFEST", str(MANIFEST)))
    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--target", type=int, default=TARGET_ROWS)
    p_stats.set_defaults(func=cmd_stats)

    p_import = sub.add_parser("import")
    p_import.add_argument("--input", required=True)
    p_import.set_defaults(func=cmd_import)

    p_label = sub.add_parser("label")
    p_label.add_argument("--annotator", required=True)
    p_label.add_argument("--labels", required=True)
    p_label.set_defaults(func=cmd_label)

    p_review = sub.add_parser("review")
    p_review.add_argument("--limit", type=int, default=20)
    p_review.set_defaults(func=cmd_review)

    p_adj = sub.add_parser("adjudicate")
    p_adj.add_argument("--id", required=True)
    p_adj.add_argument("--value", required=True, help="yes or no")
    p_adj.add_argument("--adjudicator", required=True)
    p_adj.set_defaults(func=cmd_adjudicate)

    p_freeze = sub.add_parser("freeze")
    p_freeze.set_defaults(func=cmd_freeze)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
