"""Configuration shared by the agent and the mock portal.

Everything is environment-driven so the two services can run as separate
processes (and, later, as separate deployed services) while still agreeing on
the approval-token secret.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

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

# local (default) uses the demo secrets below. production refuses to start unless both
# secrets are set, strong, and not one of the known placeholders.
ENV = os.getenv("RTI_ENV", "local").strip().lower()
if ENV not in ("local", "production"):
    raise RuntimeError("RTI_ENV must be 'local' or 'production'")

# A deployed agent that still points at a placeholder silently fails every filing, so
# production refuses to start instead.
PLACEHOLDER_HOSTS = frozenset({"example.invalid", "example.com", "example.org", "localhost.invalid"})
if ENV == "production":
    _portal = urlparse(PORTAL_URL)
    if _portal.scheme not in ("http", "https") or not _portal.hostname:
        raise RuntimeError("RTI_PORTAL_URL must be an absolute http(s) URL")
    if _portal.hostname.lower() in PLACEHOLDER_HOSTS:
        raise RuntimeError("RTI_PORTAL_URL is still a placeholder; set the portal's real URL")

DEMO_APPROVAL_SECRET = "rti-pilot-demo-secret-change-me"
DEMO_ADMIN_TOKEN = "rti-demo-admin"
DEMO_OPERATOR_TOKEN = "rti-demo-operator"
PLACEHOLDER_SECRETS = frozenset(
    {
        DEMO_APPROVAL_SECRET,
        DEMO_ADMIN_TOKEN,
        DEMO_OPERATOR_TOKEN,
        "replace-with-a-long-random-value",
    }
)
MIN_SECRET_LENGTH = 32


def _secret(name: str, demo_value: str) -> str:
    value = os.getenv(name, "").strip()
    if ENV != "production":
        return value or demo_value
    # Never include the value itself in an error message.
    if not value:
        raise RuntimeError(f"{name} must be set when RTI_ENV=production")
    if value in PLACEHOLDER_SECRETS:
        raise RuntimeError(f"{name} is a known placeholder; set a unique random value")
    if len(value) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
    return value


# Shared secret used to sign approval tokens. The portal verifies signatures;
# it never reads the agent database.
APPROVAL_SECRET = _secret("RTI_APPROVAL_SECRET", DEMO_APPROVAL_SECRET)
ADMIN_TOKEN = _secret("RTI_ADMIN_TOKEN", DEMO_ADMIN_TOKEN)
# Guards the agent's operator surface: approvals, review queue, test results and
# the CAPTCHA handoff. Without it every mutating route is open to anyone with the URL.
OPERATOR_TOKEN = _secret("RTI_OPERATOR_TOKEN", DEMO_OPERATOR_TOKEN)
if ENV == "production" and len({APPROVAL_SECRET, ADMIN_TOKEN, OPERATOR_TOKEN}) != 3:
    raise RuntimeError(
        "RTI_APPROVAL_SECRET, RTI_ADMIN_TOKEN and RTI_OPERATOR_TOKEN must all differ"
    )

# auto | playwright | http
FILING_MODE = os.getenv("RTI_FILING_MODE", "auto").lower()
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("RTI_PLAYWRIGHT_TIMEOUT_MS", "15000"))

# Optional Claude adapter. The deterministic engine is the default so the demo
# runs with no API key; setting a key switches decision-making to Claude.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
USE_LLM = os.getenv("RTI_USE_LLM", "0") == "1"

ROUTING_CONFIDENCE_THRESHOLD = float(os.getenv("RTI_ROUTING_THRESHOLD", "0.70"))

# Per-IP limit on creating runs, in requests per minute. A public demo otherwise
# lets one client fill the database and the operator queue.
RUN_RATE_LIMIT = int(os.getenv("RTI_RUN_RATE_LIMIT", "120"))

# Background deadline worker: a thread inside the agent process by default because
# the hosted deployment keeps SQLite on the instance (a separate worker would get a
# different disk). `python -m agent.worker` runs the same loop standalone.
WORKER_ENABLED = os.getenv("RTI_WORKER_ENABLED", "1") == "1"
WORKER_INTERVAL_SECONDS = float(os.getenv("RTI_WORKER_INTERVAL_SECONDS", "30"))
# How long a flagged request may wait for a human before the queue marks it overdue.
REVIEW_SLA_HOURS = int(os.getenv("RTI_REVIEW_SLA_HOURS", "48"))
# How often a filed request is re-checked against the portal.
CHECK_INTERVAL_HOURS = float(os.getenv("RTI_CHECK_INTERVAL_HOURS", "6"))

RULES_PATH = PROJECT_ROOT / "rules" / "rti_rules.json"
STATIC_DIR = PROJECT_ROOT / "agent" / "static"
