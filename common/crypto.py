"""Signed approval tokens.

The agent signs {approval_id, run_id, draft_id, fee_inr, destination,
approved_at} with a secret both services hold. The portal can then reject any
payment that was not preceded by an approval, without reading the agent's
database -- which keeps the two systems independent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from common import config

_ERROR = "invalid approval token"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_approval(payload: dict[str, Any], secret: str | None = None) -> str:
    secret = secret or config.APPROVAL_SECRET
    body = _canonical(payload)
    sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_approval(token: str, secret: str | None = None) -> dict[str, Any] | None:
    """Return the signed payload, or None when the token is invalid."""
    if not token or "." not in token:
        return None
    secret = secret or config.APPROVAL_SECRET
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception:
        return None
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def idempotency_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]
