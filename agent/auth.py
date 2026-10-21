"""Access control for the agent's HTTP surface.

Two credentials, deliberately different in scope:

* an **operator account** guards the operator surface: the review queue, stored
  test results, the tracker view and the demo CAPTCHA handoff. Accounts live in
  the database with a hashed token, so adding or revoking a reviewer does not mean
  redeploying with a new shared secret. ``RTI_OPERATOR_TOKEN`` bootstraps the first
  account; ``python -m scripts.operator`` adds and revokes the rest.
* a **run token** is minted when a citizen creates a run. It scopes that client to
  that run: read it, answer a follow-up, approve or decline its own card. It cannot
  read another run, and it cannot open the operator views.

Before this module existed every route was anonymous. On a public deployment that
meant anyone who found the URL could approve and file in someone else's name, or
read every request and its evidence. Both are now denied by default.
"""

from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException, Request

from common import config

OPERATOR_HEADER = "X-Operator-Token"
RUN_HEADER = "X-Run-Token"


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def presented_operator_token(request: Request) -> str:
    return (
        request.headers.get(OPERATOR_HEADER, "").strip()
        or _bearer(request)
    )


def presented_run_token(request: Request) -> str:
    return request.headers.get(RUN_HEADER, "").strip()


def _identity_from_row(row: Any) -> dict[str, Any]:
    return {
        "operator_id": row["operator_id"],
        "name": row["name"],
        "role": row["role"],
    }


def operator_identity(request: Request, conn: Any) -> dict[str, Any] | None:
    """Resolve the presented token to an active account, or None.

    The environment token is still accepted when no account has been created yet,
    which keeps a fresh database usable before ``scripts.operator`` has run.
    """
    from agent import db

    candidate = presented_operator_token(request)
    if not candidate:
        return None
    row = db.operator_for_token(conn, candidate)
    if row is not None:
        return _identity_from_row(row)
    # Only before any account exists. Once accounts are managed in the database, a
    # revoked or rotated token must not keep working through this fallback.
    if conn.execute("SELECT 1 FROM operators LIMIT 1").fetchone():
        return None
    if config.OPERATOR_TOKEN and candidate == config.OPERATOR_TOKEN:
        return {"operator_id": "OP-ENV", "name": "operator", "role": "admin"}
    return None


def require_operator(request: Request) -> dict[str, Any]:
    """FastAPI dependency: 401 without a valid operator account."""
    from agent import db

    conn = db.connect()
    try:
        identity = operator_identity(request, conn)
    finally:
        conn.close()
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


def authorize_run(request: Request, conn, run_id: str) -> None:
    """Allow the operator or the client holding this run's token.

    404 rather than 403 for a missing run so the route does not confirm which run
    ids exist. A wrong token for an existing run is 403: the caller already knows
    the id, and an honest error is more useful than a fake 404.
    """
    from agent import db

    try:
        db.get_run(conn, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    if operator_identity(request, conn) is not None:
        return
    if db.verify_run_token(conn, run_id, presented_run_token(request)):
        return
    raise HTTPException(status_code=403, detail="this run belongs to another client")


# ------------------------------------------------------------------ rate limit

WINDOW_SECONDS = 60.0


def reset_rate_limits() -> None:
    """Clear the shared counter. Used by tests, not by a running service."""
    from agent import db

    try:
        conn = db.connect()
        try:
            conn.execute("DELETE FROM rate_limits")
        finally:
            conn.close()
    except Exception:  # a database that does not exist yet has nothing to clear
        return


def check_run_rate(conn: Any, key: str, limit: int | None = None) -> None:
    """Sliding-window limit on run creation, per client key.

    Stored beside the data it protects, so two instances and a restart share one
    budget instead of each handing out a fresh one.
    """
    from agent import db

    limit = config.RUN_RATE_LIMIT if limit is None else limit
    db.check_rate_limit(conn, key, limit, WINDOW_SECONDS)


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def scoped_headers(run_token: str | None = None, operator_token: str | None = None) -> dict[str, str]:
    """Header dict for scripts and tests; kept here so the names have one source."""
    headers: dict[str, str] = {}
    if operator_token:
        headers[OPERATOR_HEADER] = operator_token
    if run_token:
        headers[RUN_HEADER] = run_token
    return headers


def combine(*parts: Iterable[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for part in parts:
        merged.update(part)
    return merged
