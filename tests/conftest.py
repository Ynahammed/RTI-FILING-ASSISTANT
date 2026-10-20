from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import db  # noqa: E402
from common import config  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DB", tmp_path / "agent-test.db")
    connection = db.init_db()
    yield connection
    connection.close()
