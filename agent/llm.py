"""Optional Claude review layer.

The deterministic engine is the authority for labels, exemptions, fees and the
approval gate. When `RTI_USE_LLM=1` and an API key is present, Claude is asked for
an advisory second opinion on triage; its suggestion is written to the evidence
ledger but cannot change the outcome. Any network or parsing failure is silent and
the run continues deterministically.

This path is off by default and was not exercised in the local build (no API key).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from common import config

SYSTEM = (
    "You review an Indian RTI request triage. Reply ONLY with JSON: "
    '{"suggestion":"rti|grievance|mixed|exempt|uncertain",'
    '"confidence":0.0,"reason":"one sentence","concerns":["..."]}. '
    "An RTI request asks for records that already exist. Action demands are grievances. "
    "A request for a third party's personal information is exempt under Section 8(1)(j)."
)


def enabled() -> bool:
    return bool(config.USE_LLM and config.ANTHROPIC_API_KEY)


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def review_triage(text: str, deterministic_label: str) -> dict[str, Any] | None:
    if not enabled():
        return None
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": f"Deterministic label: {deterministic_label}\nRequest:\n{text}",
                }],
            },
            timeout=20,
        )
        response.raise_for_status()
        content = response.json().get("content", [])
        if not content or content[0].get("type") != "text":
            return None
        data = _extract_json(content[0].get("text", ""))
        if not data:
            return None
        return {
            "suggestion": str(data.get("suggestion", ""))[:20],
            "confidence": float(data.get("confidence", 0) or 0),
            "reason": str(data.get("reason", ""))[:400],
            "concerns": [str(c)[:200] for c in (data.get("concerns") or [])][:4],
            "advisory_only": True,
        }
    except Exception:
        return None
