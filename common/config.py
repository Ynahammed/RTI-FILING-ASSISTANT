"""Configuration shared by the agent and the mock portal.

Everything is environment-driven so the two services can run as separate
processes (and, later, as separate deployed services) while still agreeing on
the approval-token secret.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("RTI_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

AGENT_DB = Path(os.getenv("RTI_AGENT_DB", DATA_DIR / "agent.db")).resolve()
PORTAL_DB = Path(os.getenv("RTI_PORTAL_DB", DATA_DIR / "portal.db")).resolve()

AGENT_HOST = os.getenv("RTI_AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.getenv("RTI_AGENT_PORT", "8000"))
PORTAL_HOST = os.getenv("RTI_PORTAL_HOST", "127.0.0.1")
PORTAL_PORT = int(os.getenv("RTI_PORTAL_PORT", "8001"))

PORTAL_URL = os.getenv("RTI_PORTAL_URL", f"http://{PORTAL_HOST}:{PORTAL_PORT}").rstrip("/")
AGENT_URL = os.getenv("RTI_AGENT_URL", f"http://{AGENT_HOST}:{AGENT_PORT}").rstrip("/")

# Shared secret used to sign approval tokens. The portal verifies signatures;
# it never reads the agent database.
APPROVAL_SECRET = os.getenv("RTI_APPROVAL_SECRET", "rti-pilot-demo-secret-change-me")
ADMIN_TOKEN = os.getenv("RTI_ADMIN_TOKEN", "rti-demo-admin")

# auto | playwright | http
FILING_MODE = os.getenv("RTI_FILING_MODE", "auto").lower()
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("RTI_PLAYWRIGHT_TIMEOUT_MS", "15000"))

# Optional Claude adapter. The deterministic engine is the default so the demo
# runs with no API key; setting a key switches decision-making to Claude.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
USE_LLM = os.getenv("RTI_USE_LLM", "0") == "1"

ROUTING_CONFIDENCE_THRESHOLD = float(os.getenv("RTI_ROUTING_THRESHOLD", "0.70"))

RULES_PATH = PROJECT_ROOT / "rules" / "rti_rules.json"
STATIC_DIR = PROJECT_ROOT / "agent" / "static"
