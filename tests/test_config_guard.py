"""Production secret guard in common/config.py.

Each case imports config in a fresh interpreter so module-level values are read from
exactly the environment under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STRONG_A = "a" * 40
STRONG_B = "b" * 40
STRONG_C = "c" * 40


def load(**env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("RTI_")}
    clean.update(env)
    code = "from common import config; print(config.ENV, config.APPROVAL_SECRET == config.ADMIN_TOKEN)"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
    )


def load_url(**env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("RTI_")}
    clean.update(env)
    code = "from common import config; print(config.PORTAL_URL)"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
    )


PRODUCTION_SECRETS = {
    "RTI_ENV": "production",
    "RTI_APPROVAL_SECRET": STRONG_A,
    "RTI_ADMIN_TOKEN": STRONG_B,
    "RTI_OPERATOR_TOKEN": STRONG_C,
}


def test_production_strips_a_pasted_trailing_newline_from_the_portal_url() -> None:
    # A dashboard paste carries the line break; urlparse ignores it and httpx does not,
    # so an unstripped value boots healthy and fails every filing.
    result = load_url(
        **PRODUCTION_SECRETS,
        RTI_PORTAL_URL="https://rti-pilot-portal.onrender.com/\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://rti-pilot-portal.onrender.com"


def test_production_refuses_whitespace_inside_the_portal_url() -> None:
    result = load_url(
        **PRODUCTION_SECRETS,
        RTI_PORTAL_URL="https://rti-pilot-portal.onrender.com /api",
    )
    assert result.returncode != 0
    assert "must not contain whitespace" in result.stderr


def test_local_uses_demo_defaults_without_any_env() -> None:
    result = load()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["local", "False"]


def test_local_accepts_short_custom_secrets() -> None:
    result = load(RTI_ENV="local", RTI_APPROVAL_SECRET="x", RTI_ADMIN_TOKEN="y")
    assert result.returncode == 0, result.stderr


def test_production_accepts_strong_distinct_secrets() -> None:
    result = load(
        RTI_ENV="production",
        RTI_APPROVAL_SECRET=STRONG_A,
        RTI_ADMIN_TOKEN=STRONG_B,
        RTI_OPERATOR_TOKEN=STRONG_C,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["production", "False"]


@pytest.mark.parametrize(
    "env, message",
    [
        (
            {
                "RTI_ENV": "production",
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "RTI_APPROVAL_SECRET must be set",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "RTI_ADMIN_TOKEN must be set",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": STRONG_B,
            },
            "RTI_OPERATOR_TOKEN must be set",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": "rti-pilot-demo-secret-change-me",
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "known placeholder",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": "rti-demo-admin",
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "known placeholder",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": "replace-with-a-long-random-value",
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "known placeholder",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": "short-secret",
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "at least 32",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": STRONG_A,
                "RTI_OPERATOR_TOKEN": STRONG_C,
            },
            "must all differ",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_B,
            },
            "must all differ",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": "rti-demo-operator",
            },
            "known placeholder",
        ),
        (
            {
                "RTI_ENV": "production",
                "RTI_APPROVAL_SECRET": STRONG_A,
                "RTI_ADMIN_TOKEN": STRONG_B,
                "RTI_OPERATOR_TOKEN": STRONG_C,
                "RTI_PORTAL_URL": "https://example.invalid",
            },
            "placeholder",
        ),
        ({"RTI_ENV": "staging"}, "must be 'local' or 'production'"),
    ],
)
def test_production_refuses_unsafe_configuration(env: dict[str, str], message: str) -> None:
    result = load(**env)
    assert result.returncode != 0
    assert message in result.stderr
    # The error must never echo a secret value.
    for key, value in env.items():
        if key in ("RTI_APPROVAL_SECRET", "RTI_ADMIN_TOKEN", "RTI_OPERATOR_TOKEN"):
            assert value not in result.stderr
