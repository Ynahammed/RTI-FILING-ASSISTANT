"""Advisory Section 8(1)(j) text classifier (pure-Python TF-IDF + logistic regression).

Trained by scripts/train_8_1_j_classifier.py on RTI-Bench. The weights are exported to
rules/exemption_8_1_j_model.json and scored here with no scikit-learn dependency. Scores
match scikit-learn's TfidfVectorizer + LogisticRegression (checked by the training script).

Its output is advisory only: triage records a review note when the score clears the
threshold, but it never changes the deterministic label and never blocks filing. Its
out-of-fold precision on RTI-Bench is low (see artifacts/classifier_8_1_j_results.json),
and it has not been validated on citizen-style text, so it must not be used as a gate.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from common import config

MODEL_PATH = Path(
    os.getenv("RTI_EXEMPTION_MODEL", config.PROJECT_ROOT / "rules" / "exemption_8_1_j_model.json")
)
NGRAM_RANGE = (1, 2)
# Same token pattern as scikit-learn's default (single-character tokens are dropped).
TOKEN = re.compile(r"(?u)\b\w\w+\b")


def ngrams(text: str) -> list[str]:
    """Word unigrams and bigrams, joined with a space, as scikit-learn produces them."""
    tokens = TOKEN.findall(text.lower())
    grams = list(tokens)
    if NGRAM_RANGE[1] >= 2:
        grams.extend(" ".join(tokens[i : i + 2]) for i in range(len(tokens) - 1))
    return grams


def probability(model: dict[str, Any], text: str) -> float:
    """Probability that the request matches the 8(1)(j) pattern in the training data."""
    vocab = model["vocabulary"]
    idf = model["idf"]
    coef = model["coef"]
    counts = Counter(vocab[g] for g in ngrams(text) if g in vocab)
    # Sublinear tf, then tf-idf weights, then L2 normalisation over in-vocabulary terms.
    weights = {idx: (1.0 + math.log(tf)) * idf[idx] for idx, tf in counts.items()}
    norm = math.sqrt(sum(w * w for w in weights.values()))
    z = float(model["intercept"])
    if norm:
        z += sum((w / norm) * coef[idx] for idx, w in weights.items())
    z = max(-700.0, min(700.0, z))
    return 1.0 / (1.0 + math.exp(-z))


@lru_cache(maxsize=1)
def _load() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def load_model() -> dict[str, Any] | None:
    """Public accessor for the exported weights, used by the evaluation scripts."""
    return _load()


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
    score = probability(model, text)
    threshold = float(model["threshold"])
    if score < threshold:
        return None
    return {"score": score, "threshold": threshold}
