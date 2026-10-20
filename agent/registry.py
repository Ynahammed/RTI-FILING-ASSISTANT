"""Authority registry and precedent retrieval.

Every lookup returns the registry source ID, URL and retrieval time so the
evidence timeline can show where a route came from.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from common import config

DEPARTMENT_LABELS = {
    "public_works": "Public Works Department",
    "municipal": "Urban Development / Municipal Corporation",
    "home": "Home Department (Police)",
    "health": "Public Health Department",
    "water": "Water Resources Department",
    "transport": "Transport Department",
    "revenue": "Revenue Department",
    "education": "Education Department",
    "environment": "Environment Department",
    "general_administration": "General Administration Department",
}


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    return json.loads((config.PROJECT_ROOT / "rules" / "authority_registry.json").read_text(encoding="utf-8"))


def authorities() -> list[dict[str, Any]]:
    return registry()["authorities"]


def find(level: str, state: str, department: str) -> dict[str, Any] | None:
    for auth in authorities():
        if auth["level"] == level and auth["state"] == state and auth["department"] == department:
            return auth
    return None


def by_code(code: str) -> dict[str, Any] | None:
    return next((a for a in authorities() if a["code"] == code), None)


def fallback(state: str) -> dict[str, Any] | None:
    return find("state" if state != "Central" else "central", state, "general_administration")


def precedents_for(department: str, text: str, limit: int = 1) -> list[dict[str, Any]]:
    """Cheap lexical retrieval over the three synthetic precedents."""
    from agent import db

    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM precedents WHERE department=?", (department,)))
    finally:
        conn.close()
    lowered = text.lower()
    scored: list[tuple[int, Any]] = []
    for row in rows:
        score = sum(1 for kw in row["keywords"].split(",") if kw.strip() and kw.strip() in lowered)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [dict(row) for _, row in scored[:limit]]
