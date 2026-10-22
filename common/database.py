"""Database connections behind one dialect-aware seam.

Local development and the demo use SQLite, because a file needs no service and the
whole system runs offline. A hosted deployment needs more than that: the Render
free instance keeps SQLite on ephemeral disk, so a restart loses every run, and two
replicas cannot share a file.

Both database modules (``agent.db`` and ``portal.db``) therefore talk to this seam.
Setting ``RTI_DATABASE_URL`` (or the per-service ``RTI_AGENT_DATABASE_URL`` /
``RTI_PORTAL_DATABASE_URL``) switches that service to Postgres. Nothing else in the
application changes: the same SQL is translated, the same row access works, and the
database still enforces the approval gate and the verifier lease through triggers.

The translation is deliberately small and explicit, because a silent mismatch is
worse than a loud failure:

* ``?`` placeholders become ``%s`` outside string literals.
* ``INSERT OR IGNORE`` becomes ``INSERT ... ON CONFLICT DO NOTHING``.
* ``BEGIN IMMEDIATE`` becomes ``BEGIN`` (Postgres serialises writers itself).
* ``rowid`` becomes ``ctid`` for insertion-order tie-breaks.
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` becomes ``BIGSERIAL PRIMARY KEY``.
* ``PRAGMA table_info`` and ``PRAGMA database_list`` are answered from the catalogue.
* SQLite trigger bodies are removed from the shared schema; the Postgres
  equivalents are created explicitly by each database module.

``RTI_DATABASE_URL`` applies to both services. They keep separate tables, so one
database is safe; separate databases are cleaner.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

try:  # optional: only imported when a Postgres URL is configured
    import psycopg as _psycopg
except ImportError:  # pragma: no cover - dependency is declared
    _psycopg = None

# A constraint violation arrives as sqlite3.IntegrityError locally and as a
# psycopg error on Postgres. Callers that translate a gate violation into a
# PermissionError must catch both; catching psycopg.Error broadly is safe because
# the caller re-raises anything it does not recognise.
INTEGRITY_ERRORS: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,) + (
    (_psycopg.Error,) if _psycopg is not None else ()
)

POSTGRES_SCHEMES = ("postgresql://", "postgres://")


class DatabaseConfigError(RuntimeError):
    """A configured database URL is malformed or the driver is missing."""


def is_postgres_url(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith(POSTGRES_SCHEMES)


def redact(dsn: str) -> str:
    """A connection string safe to log: credentials removed."""
    return re.sub(r"(://[^:/@]+):([^@]*)@", r"\1:***@", dsn.strip())


def describe(conn: Any) -> str:
    """A short label for logs and health output."""
    if is_postgres(conn):
        return f"postgres:{redact(getattr(conn, 'dsn', ''))}"
    return f"sqlite:{getattr(conn, 'db_path', '')}"


def is_postgres(conn: Any) -> bool:
    return getattr(conn, "dialect", "sqlite") == "postgres"


def connect(target: str | Path) -> Any:
    """Open a SQLite file or a Postgres URL, whichever the target names."""
    value = str(target)
    if is_postgres_url(value):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise DatabaseConfigError(
                "Postgres is configured but psycopg is not installed: "
                "python -m pip install 'psycopg[binary]>=3.2'"
            ) from exc
        return PostgresConnection(value)
    if value.lower().startswith(("postgres", "postgresql")):
        raise DatabaseConfigError(
            f"database URL {redact(value)} must start with postgresql:// or postgres://"
        )
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


class _StaticCursor:
    """A cursor-shaped answer for PRAGMAs that Postgres does not have."""

    rowcount = 0

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)


class PostgresConnection:
    """The small part of the sqlite3 connection API this codebase uses.

    psycopg's cursor already provides fetchone/fetchall/iteration/rowcount, and
    dict rows support both ``row["name"]`` and ``dict(row)``. What it does not
    provide is SQLite's SQL dialect, so that is translated here.
    """

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.dsn = dsn.strip()
        self._conn = psycopg.connect(
            self.dsn,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=10,
            application_name="rti-pilot",
        )

    # ------------------------------------------------------------- execution
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        pragma = _pragma(sql, self)
        if pragma is not None:
            return pragma
        return self._conn.execute(translate(sql), tuple(params or ()))

    def executescript(self, script: str) -> None:
        script = strip_sqlite_triggers(script)
        for statement in split_sql(script):
            text = statement.strip()
            if not text:
                continue
            self.execute(text)

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------- SQL helpers

_QMARK = re.compile(r"\?")
_BEGIN_IMMEDIATE = re.compile(r"\bBEGIN\s+IMMEDIATE\b", re.I)
_ROWID = re.compile(r"\browid\b", re.I)
_AUTOINCREMENT = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.I)
# SQLite's REAL is 8-byte IEEE; Postgres REAL is 4-byte and rounds 0.95 to
# 0.949999988. A threshold of exactly 0.95 then fails only on Postgres, which is
# how the follow-up loop stopped accepting an answer it had stored.
_REAL = re.compile(r"\bREAL\b", re.I)
_INSERT_OR_IGNORE = re.compile(r"(?is)^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b")


def translate(sql: str) -> str:
    """Rewrite the SQLite dialect used by this codebase into Postgres."""
    text = _AUTOINCREMENT.sub("BIGSERIAL PRIMARY KEY", sql)
    text = _REAL.sub("DOUBLE PRECISION", text)
    text = _BEGIN_IMMEDIATE.sub("BEGIN", text)
    text = _ROWID.sub("ctid", text)

    match = _INSERT_OR_IGNORE.match(text)
    if match:
        prefix = match.group(1)
        body = text[match.end():]
        text = f"{prefix}INSERT INTO{body}"
        if "ON CONFLICT" not in text.upper():
            text = text.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    return _replace_placeholders(text)


def _replace_placeholders(sql: str) -> str:
    """Replace ``?`` with ``%s`` but leave string literals untouched."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            out.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    out.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _pragma(sql: str, conn: "PostgresConnection") -> _StaticCursor | None:
    text = sql.strip()
    if not re.match(r"(?is)^PRAGMA\b", text):
        return None
    table = re.match(r"(?is)^PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", text)
    if table:
        rows = conn._conn.execute(
            """SELECT column_name AS name, data_type AS type
                 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position""",
            (table.group(1),),
        ).fetchall()
        return _StaticCursor([dict(row) for row in rows])
    if re.match(r"(?is)^PRAGMA\s+database_list\b", text):
        return _StaticCursor([{"seq": 0, "name": "main", "file": redact(conn.dsn)}])
    return _StaticCursor([])


def strip_sqlite_triggers(script: str) -> str:
    """Remove SQLite trigger blocks, whose bodies Postgres cannot parse.

    The ``BEGIN`` is part of the pattern on purpose: a Postgres trigger ends with
    ``EXECUTE FUNCTION`` and has no body, so it must survive this pass.
    """
    # ``[^;]*?`` keeps the match inside one statement: a Postgres trigger ends
    # with a semicolon before any BEGIN, so it cannot match here.
    return re.sub(
        r"(?is)CREATE\s+TRIGGER\b[^;]*?\bBEGIN\b.*?\bEND\s*;", "", script
    )


def split_sql(script: str) -> list[str]:
    """Split a script on semicolons, respecting quotes, comments and $$ bodies."""
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    dollar: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(script):
        char = script[index]
        pair = script[index:index + 2]

        if line_comment:
            buffer.append(char)
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            buffer.append(char)
            if pair == "*/":
                buffer.append("/")
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if dollar:
            if script.startswith(dollar, index):
                buffer.append(dollar)
                index += len(dollar)
                dollar = None
            else:
                buffer.append(char)
                index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    buffer.append(script[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if pair == "--":
            line_comment = True
            buffer.append(pair)
            index += 2
            continue
        if pair == "/*":
            block_comment = True
            buffer.append(pair)
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "$":
            tag = re.match(r"\$[A-Za-z0-9_]*\$", script[index:])
            if tag:
                dollar = tag.group(0)
                buffer.append(dollar)
                index += len(dollar)
                continue
        if char == ";":
            statements.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def serialise_registration(conn: Any) -> None:
    """Serialise registration-number allocation inside the current transaction.

    SQLite's ``BEGIN IMMEDIATE`` locks every writer. Postgres does not, so the
    read-then-insert in the portal needs an explicit lock or two concurrent
    payments can pick the same registration number.
    """
    if is_postgres(conn):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", ("rti-portal-registration",)
        )


def for_update_skip_locked(conn: Any) -> str:
    """The row-lock clause for a claim query, empty on SQLite."""
    return " FOR UPDATE SKIP LOCKED" if is_postgres(conn) else ""
