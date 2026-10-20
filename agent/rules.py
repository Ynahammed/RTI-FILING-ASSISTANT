"""Access to the seeded RTI rules file."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from common import config


@lru_cache(maxsize=1)
def rules() -> dict[str, Any]:
    return json.loads(config.RULES_PATH.read_text(encoding="utf-8"))


def section(ref: str) -> dict[str, Any]:
    return rules()["sections"].get(ref, {})


def fee_default() -> int:
    return int(section("fee").get("default_inr", 10))


def bpl_exempt() -> bool:
    return bool(section("fee").get("bpl_exempt", True))


def routing_threshold() -> float:
    return float(rules()["routing"].get("confidence_threshold", 0.7))


def rule_summary() -> str:
    return "Seeded RTI rules v" + str(rules().get("version", "?")) + " — " + str(rules().get("disclaimer", ""))
