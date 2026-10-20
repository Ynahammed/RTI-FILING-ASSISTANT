"""Advisory Section 8(1)(j) classifier (pure-Python Naive Bayes).

Trained by scripts/train_8_1_j_classifier.py on RTI-Bench. Its output is advisory
only: triage records a review note when the score clears the threshold, but it never
changes the deterministic label and never blocks filing. It is OFF by default
(opt in with RTI_EXEMPTION_ADVISORY=1). Its out-of-fold precision is about 0.2, and it
is trained on the RTI draft text (information_sought) while triage scores the citizen's
request text, so it must not be used as a gate.
"""

from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from common import config

MODEL_PATH = Path(
    os.getenv("RTI_EXEMPTION_MODEL", config.PROJECT_ROOT / "rules" / "exemption_8_1_j_model.json")
)
ALPHA = 1.0  # Laplace smoothing, must match the training script
TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def posterior(model: dict[str, Any], text: str) -> float:
    """Probability that the request matches the 8(1)(j) pattern in the training data."""
    pos_counts = model["pos_counts"]
    neg_counts = model["neg_counts"]
    v = model["vocab_size"]
    lp = model["log_prior_pos"]
    ln = model["log_prior_neg"]
    pt = model["pos_total"]
    nt = model["neg_total"]
    for tok in tokenize(text):
        lp += math.log((pos_counts.get(tok, 0) + ALPHA) / (pt + ALPHA * v))
        ln += math.log((neg_counts.get(tok, 0) + ALPHA) / (nt + ALPHA * v))
    diff = ln - lp
    return 1.0 / (1.0 + math.exp(max(-700.0, min(700.0, diff))))


@lru_cache(maxsize=1)
def _load() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def advisory_flag(text: str) -> dict[str, float] | None:
    """Return {score, threshold} when the advisory classifier flags the text, else None.

    Returns None unless the feature is enabled (RTI_EXEMPTION_ADVISORY=1), the model file
    exists, and the text is non-empty.
    """
    if os.getenv("RTI_EXEMPTION_ADVISORY", "0") != "1" or not text.strip():
        return None
    model = _load()
    if model is None:
        return None
    score = posterior(model, text)
    threshold = float(model["threshold"])
    if score < threshold:
        return None
    return {"score": score, "threshold": threshold}
