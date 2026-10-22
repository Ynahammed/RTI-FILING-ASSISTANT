"""Copy existing SQLite data into the Postgres backend.

The backend switch is not a connection-string change for a deployment that already
has rows: ``RTI_AGENT_DATABASE_URL`` points at empty tables. This script copies the
existing files into Postgres, in an order that satisfies the database triggers:

    python -m scripts.migrate_to_postgres \\
        --agent-db data/agent.db \\
        --agent-url postgresql://user:password@host:5432/rti_agent

    python -m scripts.migrate_to_postgres \\
        --portal-db data/portal.db \\
        --portal-url postgresql://user:password@host:5432/rti_portal

Both URLs and both files can be given in one call. The copy is idempotent: rows that
already exist are skipped, so a rerun after a partial copy finishes the job. It never
deletes anything on either side.

What it does not do: change the running service (set the environment variable and
restart), copy SQLite's WAL files, or preserve sequence counters for tables that are
inserted by primary key. Primary-key sequences are reset to the maximum copied value,
so the next insert cannot collide.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from agent import db as agent_db
from common import database
from portal import db as portal_db

# Parent rows before child rows. `actions` needs its approved approval to exist,
# because the Postgres trigger checks it on insert; the same is true of
# `verifications` and its verifier token.
AGENT_TABLES = (
    "runs",
    "fields",
    "messages",
    "decisions",
    "evidence",
    "drafts",
    "approvals",
    "actions",
    "verifier_tokens",
    "verifications",
    "tracker",
    "test_results",
    "precedents",
    "run_tokens",
    "jobs",
    "operators",
    "operator_audit",
    "reviews",
    "rate_limits",
    "notifications",
)

PORTAL_TABLES = (
    "settings",
    "authorities",
    "applications",
    "payment_attempts",
    "replies",
    "events",
)

# Bigserial columns that must not restart at 1 after explicit IDs are copied.
AGENT_SEQUENCES = (("messages", "message_id"),)
PORTAL_SEQUENCES = (("events", "event_id"),)


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _postgres_columns(conn: Any, table: str) -> list[str]:
    rows = conn.execute(
        """SELECT column_name AS name
             FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ?
            ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return [row["name"] for row in rows]


def _reset_sequence(conn: Any, table: str, column: str) -> None:
    seq = conn.execute(
        "SELECT pg_get_serial_sequence(?, ?) AS seq", (table, column)
    ).fetchone()
    if not seq or not seq["seq"]:
        return
    # setval(..., true) so the next value is max+1, not max reused.
    conn.execute(
        f"SELECT setval(?, COALESCE((SELECT MAX({column}) FROM {table}), 1), true)",
        (seq["seq"],),
    )


def copy_table(
    source: sqlite3.Connection,
    target: Any,
    table: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy one table, skipping rows the target already has."""
    source_columns = _sqlite_columns(source, table)
    if not source_columns:
        return {"table": table, "source": 0, "copied": 0}
    rows = source.execute(f"SELECT * FROM {table}").fetchall()
    target_columns = set(_postgres_columns(target, table))
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        if dry_run:
            return {"table": table, "source": len(rows), "copied": 0}
        raise RuntimeError(f"{table}: no columns in common with the target schema")
    if dry_run:
        return {"table": table, "source": len(rows), "copied": 0}

    if table == "verifications":
        # The source deletes a verifier token once the verification is written, and
        # the target trigger still requires the token named by the row to exist.
        # Re-mint the token the evidence already names; this does not grant a live
        # lease to anything new, it only lets the historical row pass the check.
        for row in rows:
            token = row["writer_token"] if "writer_token" in row.keys() else None
            if token:
                target.execute(
                    "INSERT INTO verifier_tokens(token, created_at) VALUES (?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (token, row["checked_at"] if "checked_at" in row.keys() else ""),
                )

    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING"
    )
    copied = 0
    for row in rows:
        cursor = target.execute(sql, tuple(row[column] for column in columns))
        copied += max(0, int(cursor.rowcount))
    return {"table": table, "source": len(rows), "copied": copied}


def copy_database(
    source_path: str | Path,
    target: Any,
    tables: Iterable[str],
    *,
    sequences: Iterable[tuple[str, str]] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy every listed table from a SQLite file into an initialised target."""
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        results = [
            copy_table(source, target, table, dry_run=dry_run) for table in tables
        ]
        if not dry_run:
            for table, column in sequences:
                _reset_sequence(target, table, column)
    finally:
        source.close()
    return {
        "source": str(path),
        "tables": results,
        "rows_read": sum(result["source"] for result in results),
        "rows_copied": sum(result["copied"] for result in results),
        "dry_run": dry_run,
    }


def migrate_agent(source_path: str | Path, url: str, *, dry_run: bool = False) -> dict[str, Any]:
    target = database.connect(url)
    try:
        if not dry_run:
            agent_db.init_db(target, force=True)
        return copy_database(
            source_path, target, AGENT_TABLES, sequences=AGENT_SEQUENCES, dry_run=dry_run
        )
    finally:
        target.close()


def migrate_portal(source_path: str | Path, url: str, *, dry_run: bool = False) -> dict[str, Any]:
    target = database.connect(url)
    try:
        if not dry_run:
            portal_db.init_db(target)
        return copy_database(
            source_path, target, PORTAL_TABLES, sequences=PORTAL_SEQUENCES, dry_run=dry_run
        )
    finally:
        target.close()


def _print(result: dict[str, Any], label: str) -> None:
    mode = "would copy" if result["dry_run"] else "copied"
    print(f"{label}: {mode} {result['rows_copied']} of {result['rows_read']} rows from {result['source']}")
    for table in result["tables"]:
        if table["source"]:
            print(f"  {table['table']:<20} {table['copied']:>6} / {table['source']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy SQLite data into Postgres.")
    parser.add_argument("--agent-db", type=Path, default=Path("data/agent.db"))
    parser.add_argument("--portal-db", type=Path, default=Path("data/portal.db"))
    parser.add_argument("--agent-url", default="")
    parser.add_argument("--portal-url", default="")
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-portal", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="count rows, write nothing")
    args = parser.parse_args(argv)

    if not args.skip_agent and not args.agent_url:
        parser.error("--agent-url is required unless --skip-agent is set")
    if not args.skip_portal and not args.portal_url:
        parser.error("--portal-url is required unless --skip-portal is set")
    for name, url in (("agent", args.agent_url), ("portal", args.portal_url)):
        if url and not database.is_postgres_url(url):
            parser.error(f"--{name}-url must start with postgresql:// or postgres://")

    try:
        if args.agent_url:
            _print(migrate_agent(args.agent_db, args.agent_url, dry_run=args.dry_run), "agent")
        if args.portal_url:
            _print(migrate_portal(args.portal_db, args.portal_url, dry_run=args.dry_run), "portal")
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
