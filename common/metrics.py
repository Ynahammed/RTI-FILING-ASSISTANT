"""Pure-Python evaluation metrics shared by the benchmarks and their tests.

No numpy or scikit-learn here on purpose: the same functions score the model in
scripts/ and are unit-tested in tests/, and production images do not install
scikit-learn. Every function takes plain Python values and returns plain floats.

Conventions
-----------
* Scores are floats where higher means "more likely positive".
* `y_true` is a sequence of 0/1 ints or bools.
* Ranking metrics take a ranked list of item ids and a set of relevant ids; k is
  inclusive (Recall@10 inspects the first 10 ranks).
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Iterable, Sequence


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    """Precision, recall and F1 from raw counts, rounding to 4 places."""
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def micro_macro_f1(
    predicted: Sequence[set[str]],
    truth: Sequence[set[str]],
    labels: Iterable[str],
) -> dict[str, float | int]:
    """Micro and macro F1 for multi-label predictions over a fixed label set."""
    labels = list(labels)
    per_label: dict[str, dict[str, float | int]] = {}
    tp_total = fp_total = fn_total = 0
    for label in labels:
        tp = sum(label in p and label in t for p, t in zip(predicted, truth))
        fp = sum(label in p and label not in t for p, t in zip(predicted, truth))
        fn = sum(label not in p and label in t for p, t in zip(predicted, truth))
        per_label[label] = prf(tp, fp, fn)
        tp_total += tp
        fp_total += fp
        fn_total += fn
    micro = prf(tp_total, fp_total, fn_total)
    macro_f1 = round(sum(float(m["f1"]) for m in per_label.values()) / len(labels), 4) if labels else 0.0
    return {
        "micro_precision": micro["precision"],
        "micro_recall": micro["recall"],
        "micro_f1": micro["f1"],
        "macro_f1": macro_f1,
        "tp": tp_total,
        "fp": fp_total,
        "fn": fn_total,
        "per_label": per_label,
    }


def hamming_loss(
    predicted: Sequence[set[str]], truth: Sequence[set[str]], labels: Iterable[str]
) -> float:
    """Fraction of label decisions that are wrong, averaged over rows and labels."""
    labels = list(labels)
    if not labels or not predicted:
        return 0.0
    wrong = sum(
        len(set(labels) & (p ^ t)) for p, t in zip(predicted, truth)
    )
    return round(wrong / (len(labels) * len(predicted)), 4)


def subset_accuracy(predicted: Sequence[set[str]], truth: Sequence[set[str]]) -> float:
    """Fraction of rows whose predicted label set exactly matches the truth."""
    if not predicted:
        return 0.0
    return round(sum(set(p) == set(t) for p, t in zip(predicted, truth)) / len(predicted), 4)


def _as_floats(values: Sequence[float]) -> list[float]:
    return [float(v) for v in values]


def average_precision(y_true: Sequence[int], scores: Sequence[float]) -> float:
    """Average precision: mean precision at each positive, ranked by score.

    Ties are broken by original order, which keeps the metric deterministic.
    """
    y = [int(v) for v in y_true]
    positives = sum(y)
    if positives == 0:
        return 0.0
    order = sorted(range(len(scores)), key=lambda i: (-float(scores[i]), i))
    hits = 0
    total = 0.0
    for rank, idx in enumerate(order, start=1):
        if y[idx]:
            hits += 1
            total += hits / rank
    return round(total / positives, 4)


def roc_auc(y_true: Sequence[int], scores: Sequence[float]) -> float:
    """Area under the ROC curve using average ranks for ties."""
    y = [int(v) for v in y_true]
    positives = sum(y)
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return 0.0
    # Average rank (1-based) for every score, so ties share one rank.
    order = sorted(range(len(scores)), key=lambda i: float(scores[i]))
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and float(scores[order[j + 1]]) == float(scores[order[i]]):
            j += 1
        avg_rank = (i + 1 + j + 1) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    rank_sum = sum(r for r, label in zip(ranks, y) if label)
    return round((rank_sum - positives * (positives + 1) / 2) / (positives * negatives), 4)


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Share of relevant items that appear in the first k ranks."""
    if not relevant:
        return 0.0
    return round(len(set(ranked[:k]) & relevant) / len(relevant), 4)


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Share of the first k ranks that are relevant."""
    if k <= 0:
        return 0.0
    return round(sum(1 for item in ranked[:k] if item in relevant) / k, 4)


def mrr(ranked: Sequence[str], relevant: set[str], k: int | None = None) -> float:
    """Reciprocal rank of the first relevant item, 0 when none is found."""
    limit = len(ranked) if k is None else min(k, len(ranked))
    for position, item in enumerate(ranked[:limit], start=1):
        if item in relevant:
            return round(1.0 / position, 4)
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Binary-relevance nDCG at k."""
    if not relevant or k <= 0:
        return 0.0
    dcg = sum(1 / math.log2(position + 1) for position, item in enumerate(ranked[:k], start=1) if item in relevant)
    ideal = sum(1 / math.log2(position + 1) for position in range(1, min(k, len(relevant)) + 1))
    return round(dcg / ideal, 4) if ideal else 0.0


def brier(y_true: Sequence[int], scores: Sequence[float]) -> float:
    """Mean squared error between predicted probabilities and outcomes."""
    if not y_true:
        return 0.0
    return round(sum((float(s) - int(y)) ** 2 for y, s in zip(y_true, scores)) / len(y_true), 4)


def expected_calibration_error(
    y_true: Sequence[int], scores: Sequence[float], bins: int = 10
) -> float:
    """ECE over equal-width probability bins: sum |mean score - accuracy| per bin."""
    if not y_true:
        return 0.0
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for y, s in zip(y_true, scores):
        s = min(max(float(s), 0.0), 1.0)
        idx = min(int(s * bins), bins - 1)
        buckets[idx].append((s, int(y)))
    total = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = sum(s for s, _ in bucket) / len(bucket)
        accuracy = sum(y for _, y in bucket) / len(bucket)
        total += len(bucket) / len(y_true) * abs(confidence - accuracy)
    return round(total, 4)


def cohen_kappa(labels_a: Sequence[bool], labels_b: Sequence[bool]) -> float:
    """Chance-corrected agreement between two annotators on binary labels.

    Kappa near 1 means the two annotators agree far beyond chance; near 0 means
    they are no better than independent coin flips with the same marginals.
    """
    n = min(len(labels_a), len(labels_b))
    if n == 0:
        return 0.0
    a = [bool(v) for v in labels_a[:n]]
    b = [bool(v) for v in labels_b[:n]]
    observed = sum(x == y for x, y in zip(a, b)) / n
    p_a = sum(a) / n
    p_b = sum(b) / n
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    if expected >= 1.0:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean rounded to 4 places, 0.0 for an empty sequence."""
    return round(sum(float(v) for v in values) / len(values), 4) if values else 0.0


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion.

    Used instead of the normal approximation because small studies and proportions
    near 0 or 1 are exactly where the normal interval misleads.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


def bootstrap_interval(
    values: Sequence[Any],
    statistic: Callable[[Sequence[Any]], float],
    iterations: int = 2000,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap interval for a statistic over rows.

    Deterministic for a given seed, so a reported interval can be reproduced.
    Resampling rows (not labels) keeps paired structure, which matters for kappa.
    """
    n = len(values)
    if n < 2 or iterations < 100:
        return (0.0, 0.0)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        draws.append(float(statistic(sample)))
    draws.sort()
    low = draws[max(0, int(0.025 * iterations) - 1)]
    high = draws[min(iterations - 1, int(0.975 * iterations))]
    return (round(low, 4), round(high, 4))
