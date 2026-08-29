"""Inter-rater agreement, for calibrating an LLM judge against human labels.

An LLM-as-judge is an unvalidated measuring instrument until its agreement
with human assessment has been quantified. This module supplies the
quantification, and it is the direct answer to the objection ValKit will meet
in every quality review: *our QA will not accept a model grading a model*.

The answer is that the judge is not trusted, it is calibrated. A human-labelled
subset of the golden set is scored by both the judge and a qualified human
reviewer, Cohen's kappa is computed over that subset, and sign-off is blocked
when kappa falls below a threshold the customer sets in their specification.
The judge's outputs are evidence only to the extent that agreement supports
them.

Cohen's kappa corrects raw agreement for the agreement expected by chance:

    kappa = (p_observed - p_expected) / (1 - p_expected)

Interpreting kappa
------------------
The conventional Landis and Koch bands (poor below 0, slight to 0.20, fair to
0.40, moderate to 0.60, substantial to 0.80, almost perfect above) are
descriptive conventions, not thresholds with statistical authority, and ValKit
defaults to requiring 0.80 rather than treating "substantial" as sufficient.

Kappa has two well-documented pathologies that matter here, both surfaced by
:func:`kappa_diagnostics` rather than left for a reviewer to discover:

*The prevalence problem.* When one label dominates — and on a well-built
golden set most cases pass, so they do — kappa is pessimistic. Two raters can
agree on 95% of cases and still score a mediocre kappa, because chance
agreement is high when almost everything is one class.

*The bias problem.* When the two raters' marginal distributions differ
systematically, kappa falls even where agreement is high.

Reporting kappa without prevalence and bias indices alongside it therefore
risks both false alarm and false comfort, which is why the credibility report
renders all three.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from ..errors import AcceptanceError
from .special import normal_quantile

__all__ = [
    "AgreementSummary",
    "cohen_kappa",
    "weighted_kappa",
    "percent_agreement",
    "confusion_matrix",
    "kappa_standard_error",
    "kappa_confidence_interval",
    "kappa_diagnostics",
    "summarise_agreement",
]


@dataclass(frozen=True)
class AgreementSummary:
    """Everything a credibility report needs to say about judge calibration."""

    n: int
    kappa: float
    percent_agreement: float
    standard_error: float
    lower: float
    upper: float
    confidence: float
    prevalence_index: float
    bias_index: float
    confusion: dict[str, int] = field(default_factory=dict)
    interpretation: str = ""
    caveats: list[str] = field(default_factory=list)


def _check_pair(a: Sequence, b: Sequence) -> None:
    if len(a) != len(b):
        raise AcceptanceError(
            f"rater sequences must be the same length, got {len(a)} and {len(b)}"
        )
    if not a:
        raise AcceptanceError("agreement is undefined for an empty sample")


def percent_agreement(rater_a: Sequence, rater_b: Sequence) -> float:
    """Raw proportion of items the two raters scored identically."""
    _check_pair(rater_a, rater_b)
    matches = sum(1 for x, y in zip(rater_a, rater_b) if x == y)
    return matches / len(rater_a)


def cohen_kappa(rater_a: Sequence, rater_b: Sequence, labels: Sequence | None = None) -> float:
    """Cohen's kappa for two raters over categorical labels.

    Degenerate cases are resolved by explicit convention rather than left to
    produce a division by zero. When chance agreement is total — both raters
    placed every item in the same single category — kappa is undefined by its
    formula. ValKit returns 1.0 when the raters nonetheless agreed on every
    item, and 0.0 otherwise, and :func:`kappa_diagnostics` records a caveat so
    the degenerate case is visible in the report rather than presented as a
    perfect score.
    """
    _check_pair(rater_a, rater_b)
    categories = list(labels) if labels is not None else sorted(
        set(rater_a) | set(rater_b), key=repr
    )

    n = len(rater_a)
    observed = percent_agreement(rater_a, rater_b)

    count_a = Counter(rater_a)
    count_b = Counter(rater_b)
    expected = sum((count_a[c] / n) * (count_b[c] / n) for c in categories)

    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def weighted_kappa(
    rater_a: Sequence[float],
    rater_b: Sequence[float],
    weights: str = "linear",
    categories: Sequence[float] | None = None,
) -> float:
    """Weighted kappa for ordinal scores, penalising by distance.

    Appropriate when a judge scores on a scale rather than pass/fail, where
    disagreeing by one point is less serious than disagreeing by four.
    ``weights`` is ``"linear"`` or ``"quadratic"``.
    """
    _check_pair(rater_a, rater_b)
    if weights not in {"linear", "quadratic"}:
        raise AcceptanceError(f"weights must be 'linear' or 'quadratic', got {weights!r}")

    levels = sorted(set(rater_a) | set(rater_b)) if categories is None else sorted(categories)
    index = {value: position for position, value in enumerate(levels)}
    size = len(levels)
    if size < 2:
        return 1.0 if percent_agreement(rater_a, rater_b) >= 1.0 else 0.0

    n = len(rater_a)
    span = size - 1

    def weight(i: int, j: int) -> float:
        distance = abs(i - j) / span
        return distance if weights == "linear" else distance * distance

    observed_matrix = [[0.0] * size for _ in range(size)]
    for x, y in zip(rater_a, rater_b):
        observed_matrix[index[x]][index[y]] += 1.0 / n

    count_a = Counter(index[v] for v in rater_a)
    count_b = Counter(index[v] for v in rater_b)

    observed_disagreement = sum(
        weight(i, j) * observed_matrix[i][j] for i in range(size) for j in range(size)
    )
    expected_disagreement = sum(
        weight(i, j) * (count_a[i] / n) * (count_b[j] / n)
        for i in range(size)
        for j in range(size)
    )

    if expected_disagreement == 0.0:
        return 1.0 if observed_disagreement == 0.0 else 0.0
    return 1.0 - observed_disagreement / expected_disagreement


def confusion_matrix(reference: Sequence, prediction: Sequence) -> dict[str, int]:
    """Binary confusion counts, with the human labels as the reference.

    The argument order matters and is not symmetric: ``reference`` is the human
    label, ``prediction`` is the judge's verdict. A false positive is therefore
    a case the judge passed that the human failed — the direction that matters
    most in a GxP setting, because it is the one where a defect reaches a
    signed report unchallenged.

    Values are coerced to booleans, so 1/0, True/False and 1.0/0.0 are all
    accepted.
    """
    _check_pair(reference, prediction)
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for actual, predicted in zip(reference, prediction):
        truth = bool(actual)
        guess = bool(predicted)
        if truth and guess:
            counts["tp"] += 1
        elif not truth and guess:
            counts["fp"] += 1
        elif truth and not guess:
            counts["fn"] += 1
        else:
            counts["tn"] += 1
    return counts


def kappa_standard_error(rater_a: Sequence, rater_b: Sequence) -> float:
    """Large-sample standard error of Cohen's kappa (Fleiss's approximation).

    Adequate for the sample sizes a calibration subset reaches; it is an
    asymptotic result and should not be leaned on below about thirty cases,
    which is why the specification's ``min_samples`` defaults to that.
    """
    _check_pair(rater_a, rater_b)
    n = len(rater_a)
    observed = percent_agreement(rater_a, rater_b)
    count_a = Counter(rater_a)
    count_b = Counter(rater_b)
    categories = set(rater_a) | set(rater_b)
    expected = sum((count_a[c] / n) * (count_b[c] / n) for c in categories)

    if expected >= 1.0:
        return 0.0
    denominator = n * (1.0 - expected) ** 2
    if denominator <= 0.0:
        return 0.0
    return math.sqrt(observed * (1.0 - observed) / denominator)


def kappa_confidence_interval(
    rater_a: Sequence, rater_b: Sequence, confidence: float = 0.95
) -> tuple[float, float]:
    """Normal-approximation confidence interval for kappa."""
    if not 0.0 < confidence < 1.0:
        raise AcceptanceError(f"confidence must be strictly between 0 and 1, got {confidence}")
    kappa = cohen_kappa(rater_a, rater_b)
    standard_error = kappa_standard_error(rater_a, rater_b)
    z = normal_quantile(1.0 - (1.0 - confidence) / 2.0)
    return (
        max(-1.0, kappa - z * standard_error),
        min(1.0, kappa + z * standard_error),
    )


def kappa_diagnostics(reference: Sequence, prediction: Sequence) -> tuple[float, float, list[str]]:
    """Prevalence index, bias index, and caveats about interpreting kappa.

    The prevalence index is ``(tp - tn) / n`` and the bias index is
    ``(fp - fn) / n``; both are zero in the ideal case and both drag kappa down
    as they grow. Reporting them alongside kappa is what stops a reviewer from
    reading a depressed kappa on a heavily skewed golden set as evidence the
    judge is unreliable, or a flattering kappa as evidence it is sound.
    """
    counts = confusion_matrix(reference, prediction)
    n = len(reference)
    prevalence = (counts["tp"] - counts["tn"]) / n
    bias = (counts["fp"] - counts["fn"]) / n

    caveats: list[str] = []
    if abs(prevalence) > 0.7:
        caveats.append(
            f"Label prevalence is highly skewed (prevalence index {prevalence:+.2f}). "
            "Kappa is pessimistic under skew; percent agreement and the confusion "
            "counts should be read alongside it."
        )
    if abs(bias) > 0.2:
        caveats.append(
            f"The judge and the human reviewer have systematically different marginals "
            f"(bias index {bias:+.2f}), which depresses kappa independently of accuracy."
        )
    if counts["fp"] > counts["fn"]:
        caveats.append(
            f"The judge passed {counts['fp']} case(s) the human reviewer failed. "
            "False passes are the consequential direction of error: they allow a "
            "defect into a signed report."
        )
    if len(set(reference)) < 2:
        caveats.append(
            "The human labels contain only one class, so kappa carries no information "
            "about discrimination. Calibrate against a subset containing both passing "
            "and failing cases."
        )
    return prevalence, bias, caveats


_LANDIS_KOCH = (
    (0.00, "poor"),
    (0.20, "slight"),
    (0.40, "fair"),
    (0.60, "moderate"),
    (0.80, "substantial"),
    (1.01, "almost perfect"),
)


def _interpret(kappa: float) -> str:
    for upper, label in _LANDIS_KOCH:
        if kappa < upper:
            return label
    return "almost perfect"


def summarise_agreement(
    reference: Sequence, prediction: Sequence, confidence: float = 0.95
) -> AgreementSummary:
    """Full agreement summary for the credibility report."""
    _check_pair(reference, prediction)
    kappa = cohen_kappa(reference, prediction)
    standard_error = kappa_standard_error(reference, prediction)
    lower, upper = kappa_confidence_interval(reference, prediction, confidence)
    prevalence, bias, caveats = kappa_diagnostics(reference, prediction)
    return AgreementSummary(
        n=len(reference),
        kappa=kappa,
        percent_agreement=percent_agreement(reference, prediction),
        standard_error=standard_error,
        lower=lower,
        upper=upper,
        confidence=confidence,
        prevalence_index=prevalence,
        bias_index=bias,
        confusion=confusion_matrix(reference, prediction),
        interpretation=_interpret(kappa),
        caveats=caveats,
    )
