"""Binomial confidence bounds and sample sizing.

This module answers the question that every acceptance criterion in a ValKit
validation package reduces to: *the agent passed k of n cases; what can we
honestly claim about its true pass rate?*

The answer is never the observed proportion. An agent that passes 176 of 180
cases has an observed rate of 0.978, but the claim that goes into a signed OQ
report is the **one-sided lower confidence bound** — the value the true rate
exceeds with the stated confidence. That distinction is the whole basis of a
defensible acceptance argument, and stating the point estimate where a bound
belongs is the most common way a validation package fails review.

One-sided versus two-sided alpha
--------------------------------
A "95% lower bound" puts the entire 5% of allowable error in the lower tail:
alpha = 1 - confidence = 0.05. A 95% *interval* splits it, 0.025 in each tail.
The two are different numbers, and using an interval's lower limit where a
one-sided bound was intended silently overstates the confidence. Every
function here is explicit about which it computes, and the one-sided
functions take ``confidence`` and put ``1 - confidence`` in the single tail.

Method selection
----------------
``clopper_pearson_lower``
    Exact, derived from the binomial tail directly. Guaranteed to have at
    least the nominal coverage, at the cost of being conservative. This is the
    default for GxP acceptance because being conservative is the correct
    direction to err when the consequence of overstating performance is a
    patient-safety or data-integrity failure.
``wilson_lower``
    Score-based, much tighter than Wald near 0 and 1, with coverage close to
    nominal on average but occasionally below it. Appropriate where the
    conservatism of Clopper-Pearson would demand an unreasonable golden set,
    and defensible provided the choice is documented in the validation plan.
``jeffreys_lower``
    The Bayesian interval with the non-informative Jeffreys prior; a middle
    ground between the two above.
``wald_lower``
    Provided for completeness and comparison only. It is badly behaved for
    small n or extreme p — at k = n it returns the point estimate with zero
    width, claiming certainty from a finite sample — and should not be used
    for an acceptance claim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import AcceptanceError
from .special import (
    inverse_regularized_incomplete_beta,
    normal_quantile,
    student_t_quantile,
)

__all__ = [
    "ConfidenceInterval",
    "NonInferiorityResult",
    "wilson_interval",
    "wilson_lower",
    "clopper_pearson_interval",
    "clopper_pearson_lower",
    "jeffreys_lower",
    "wald_lower",
    "agresti_coull_interval",
    "student_t_mean_lower",
    "min_n_zero_failures",
    "min_n_with_failures",
    "max_failures_for_n",
    "non_inferiority",
    "additional_passes_needed",
]


@dataclass(frozen=True)
class ConfidenceInterval:
    """A confidence interval or one-sided bound on a proportion."""

    lower: float
    upper: float
    confidence: float
    method: str
    two_sided: bool
    k: int
    n: int

    @property
    def point_estimate(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def __iter__(self):
        """Unpack as ``lo, hi``."""
        yield self.lower
        yield self.upper


@dataclass(frozen=True)
class NonInferiorityResult:
    """Whether an agent is non-inferior to a baseline within a margin."""

    passed: bool
    lower_bound: float
    threshold: float
    baseline: float
    margin: float
    confidence: float
    method: str
    k: int
    n: int
    rationale: str


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------


def _validate(k: int, n: int, confidence: float) -> None:
    if not isinstance(k, int) or not isinstance(n, int):
        raise AcceptanceError(f"k and n must be integers, got k={k!r}, n={n!r}")
    if n <= 0:
        raise AcceptanceError(
            f"a confidence bound needs at least one scored sample, got n={n}. "
            "A metric with no scorable samples must be reported as a failure, "
            "not as a bound of zero."
        )
    if k < 0 or k > n:
        raise AcceptanceError(f"k must satisfy 0 <= k <= n, got k={k}, n={n}")
    if not 0.0 < confidence < 1.0:
        raise AcceptanceError(f"confidence must be strictly between 0 and 1, got {confidence}")


def _validate_target(target: float) -> None:
    if not 0.0 < target < 1.0:
        raise AcceptanceError(
            f"target pass rate must be strictly between 0 and 1, got {target}. "
            "A target of 1.0 cannot be demonstrated by a finite sample."
        )


# --------------------------------------------------------------------------
# Wilson score interval
# --------------------------------------------------------------------------


def wilson_interval(k: int, n: int, confidence: float = 0.95, two_sided: bool = True) -> ConfidenceInterval:
    """Wilson score interval for a binomial proportion.

    With ``two_sided=False`` the full alpha is placed in the lower tail and the
    upper limit is 1.0, giving the one-sided bound used for acceptance.
    """
    _validate(k, n, confidence)
    z = normal_quantile(1.0 - (1.0 - confidence) / 2.0) if two_sided else normal_quantile(confidence)

    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    half_width = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))

    # At p = 0 the centre and half-width are equal in exact arithmetic, so the
    # lower limit is exactly zero; likewise the upper limit at p = 1. In
    # floating point the subtraction leaves a residue around 1e-17, which is
    # numerically harmless but would be rendered verbatim into a signed
    # document. Snap the two exact boundary cases rather than print noise.
    lower = 0.0 if k == 0 else max(0.0, center - half_width)
    if not two_sided:
        upper = 1.0
    else:
        upper = 1.0 if k == n else min(1.0, center + half_width)
    return ConfidenceInterval(lower, upper, confidence, "wilson", two_sided, k, n)


def wilson_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Wilson score lower bound: alpha = 1 - confidence in one tail."""
    return wilson_interval(k, n, confidence, two_sided=False).lower


# --------------------------------------------------------------------------
# Clopper-Pearson exact interval
# --------------------------------------------------------------------------


def clopper_pearson_interval(
    k: int, n: int, confidence: float = 0.95, two_sided: bool = True
) -> ConfidenceInterval:
    """Clopper-Pearson exact interval, from the Beta quantile function.

    The limits are the Beta quantiles that make the binomial tail probability
    equal alpha, which is why this is called exact: no normal approximation is
    involved anywhere. The endpoints degenerate correctly, and must — at k = 0
    the lower limit is exactly 0 (no evidence excludes a true rate of zero) and
    at k = n the upper limit is exactly 1.
    """
    _validate(k, n, confidence)
    alpha = (1.0 - confidence) / 2.0 if two_sided else (1.0 - confidence)

    lower = 0.0 if k == 0 else inverse_regularized_incomplete_beta(k, n - k + 1, alpha)
    if not two_sided:
        upper = 1.0
    else:
        upper = 1.0 if k == n else inverse_regularized_incomplete_beta(k + 1, n - k, 1.0 - alpha)

    return ConfidenceInterval(lower, upper, confidence, "clopper_pearson", two_sided, k, n)


def clopper_pearson_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Clopper-Pearson exact lower bound. The GxP default."""
    return clopper_pearson_interval(k, n, confidence, two_sided=False).lower


# --------------------------------------------------------------------------
# Other bounds
# --------------------------------------------------------------------------


def jeffreys_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Jeffreys (Beta(1/2, 1/2) prior) lower bound."""
    _validate(k, n, confidence)
    if k == 0:
        return 0.0
    return inverse_regularized_incomplete_beta(k + 0.5, n - k + 0.5, 1.0 - confidence)


def wald_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Wald (normal approximation) lower bound.

    Included for comparison against the defensible methods. At k = n it
    returns 1.0, asserting certainty from a finite sample, which is precisely
    why it must not be used to support an acceptance claim.
    """
    _validate(k, n, confidence)
    p = k / n
    z = normal_quantile(confidence)
    return max(0.0, p - z * math.sqrt(p * (1.0 - p) / n))


def agresti_coull_interval(k: int, n: int, confidence: float = 0.95) -> ConfidenceInterval:
    """Agresti-Coull "add two successes and two failures" interval."""
    _validate(k, n, confidence)
    z = normal_quantile(1.0 - (1.0 - confidence) / 2.0)
    n_tilde = n + z * z
    p_tilde = (k + z * z / 2.0) / n_tilde
    half_width = z * math.sqrt(p_tilde * (1.0 - p_tilde) / n_tilde)
    return ConfidenceInterval(
        max(0.0, p_tilde - half_width),
        min(1.0, p_tilde + half_width),
        confidence,
        "agresti_coull",
        True,
        k,
        n,
    )


def student_t_mean_lower(
    values: list[float] | tuple[float, ...], confidence: float = 0.95
) -> float:
    """One-sided Student-t lower confidence bound on a mean.

    Used for continuous acceptance metrics, where the binomial machinery does
    not apply. Requires at least two observations: a single observation
    carries no information about the spread and therefore supports no bound.
    """
    n = len(values)
    if n < 2:
        raise AcceptanceError(
            f"a lower bound on a mean requires at least 2 observations, got {n}"
        )
    if not 0.0 < confidence < 1.0:
        raise AcceptanceError(f"confidence must be strictly between 0 and 1, got {confidence}")

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance == 0.0:
        return mean
    standard_error = math.sqrt(variance / n)
    t = student_t_quantile(confidence, n - 1)
    return mean - t * standard_error


# --------------------------------------------------------------------------
# Sample sizing
# --------------------------------------------------------------------------


def min_n_zero_failures(target: float, confidence: float = 0.95) -> int:
    """Smallest n that demonstrates a rate >= ``target`` when every case passes.

    With zero failures the exact binomial bound reduces to a closed form: the
    probability of observing n consecutive passes from a process whose true
    rate is exactly ``target`` is ``target ** n``, so the smallest n for which
    that probability drops below ``1 - confidence`` is

        n >= ln(1 - confidence) / ln(target)

    At 95% confidence this gives the numbers a validation lead needs before
    committing to a golden set: 59 cases for a 0.95 target, 149 for 0.98, and
    299 for 0.99. Those figures assume every case passes; allowing failures
    raises them sharply (see :func:`min_n_with_failures`).
    """
    _validate_target(target)
    if not 0.0 < confidence < 1.0:
        raise AcceptanceError(f"confidence must be strictly between 0 and 1, got {confidence}")
    return math.ceil(math.log(1.0 - confidence) / math.log(target))


def min_n_with_failures(target: float, confidence: float = 0.95, failures: int = 0) -> int:
    """Smallest n whose Clopper-Pearson lower bound clears ``target`` with ``failures``.

    Searches upward from the zero-failure size, which is a strict lower bound
    on the answer. At 95% confidence and a 0.95 target this gives 59 cases with
    no failures, 93 with one, and 124 with two: tolerating a single failure
    costs more than half as many cases again, which is the argument for curating
    a golden set rather than enlarging it.
    """
    _validate_target(target)
    if failures < 0:
        raise AcceptanceError(f"failures must be non-negative, got {failures}")
    if failures == 0:
        return min_n_zero_failures(target, confidence)

    n = max(min_n_zero_failures(target, confidence), failures + 1)
    # The bound is monotone in n for fixed failures, so the first n that clears
    # the target is the answer. The ceiling guards against a non-terminating
    # search if a caller passes a target that cannot be reached in practice.
    ceiling = 10_000_000
    while n <= ceiling:
        if clopper_pearson_lower(n - failures, n, confidence) >= target:
            return n
        n += 1
    raise AcceptanceError(
        f"no sample size below {ceiling} demonstrates a rate of {target} at "
        f"{confidence} confidence while allowing {failures} failures"
    )


def max_failures_for_n(n: int, target: float, confidence: float = 0.95) -> int:
    """The most failures tolerable in ``n`` cases while still clearing ``target``.

    Returns -1 when even a perfect run of ``n`` cases cannot demonstrate the
    target, which means the golden set is too small for the acceptance
    criterion as written. That is a finding worth surfacing before a run rather
    than after: no evaluation result can rescue an under-powered test.
    """
    _validate_target(target)
    if n <= 0:
        raise AcceptanceError(f"n must be positive, got {n}")
    if clopper_pearson_lower(n, n, confidence) < target:
        return -1
    for failures in range(0, n + 1):
        if clopper_pearson_lower(n - failures, n, confidence) < target:
            return failures - 1
    return n


def additional_passes_needed(
    k: int, n: int, target: float, confidence: float = 0.95, method: str = "clopper_pearson"
) -> int:
    """How many further consecutive passes would bring the bound up to ``target``.

    The first question a validation lead asks about a failing metric is "how
    much more evidence do I need?". This answers it directly, assuming the
    additional cases all pass. Returns 0 when the target is already met.
    """
    _validate(k, n, confidence)
    _validate_target(target)
    bound = clopper_pearson_lower if method == "clopper_pearson" else wilson_lower
    if bound(k, n, confidence) >= target:
        return 0
    extra = 0
    ceiling = 1_000_000
    while extra < ceiling:
        extra += 1
        if bound(k + extra, n + extra, confidence) >= target:
            return extra
    raise AcceptanceError(
        f"the target {target} is unreachable from {k}/{n} by adding passing cases; "
        "the observed failures alone hold the bound below the target"
    )


# --------------------------------------------------------------------------
# Non-inferiority
# --------------------------------------------------------------------------


def non_inferiority(
    k: int,
    n: int,
    baseline: float,
    margin: float,
    confidence: float = 0.95,
    method: str = "clopper_pearson",
) -> NonInferiorityResult:
    """Test whether the agent is non-inferior to a baseline within ``margin``.

    The relevant question for an assistive agent is rarely "is it perfect" but
    "is it at least as good as the human process it supports, allowing a
    pre-specified margin". Non-inferiority holds when the one-sided lower bound
    on the agent's rate exceeds ``baseline - margin``.

    The margin must be justified in the validation plan before the run, not
    chosen afterwards to fit the result; that ordering is what separates a
    non-inferiority argument from a rationalisation.
    """
    _validate(k, n, confidence)
    if not 0.0 <= baseline <= 1.0:
        raise AcceptanceError(f"baseline must be between 0 and 1, got {baseline}")
    if margin < 0.0:
        raise AcceptanceError(f"non-inferiority margin must be non-negative, got {margin}")

    bound = clopper_pearson_lower if method == "clopper_pearson" else wilson_lower
    lower = bound(k, n, confidence)
    threshold = baseline - margin
    passed = lower >= threshold

    rationale = (
        f"Observed {k}/{n} (p-hat={k / n:.4f}). One-sided {confidence:.0%} "
        f"{method.replace('_', '-')} lower bound = {lower:.4f}. Non-inferiority "
        f"threshold = baseline {baseline:.4f} - margin {margin:.4f} = {threshold:.4f}. "
        f"{'Non-inferiority demonstrated' if passed else 'Non-inferiority NOT demonstrated'}."
    )
    return NonInferiorityResult(
        passed=passed,
        lower_bound=lower,
        threshold=threshold,
        baseline=baseline,
        margin=margin,
        confidence=confidence,
        method=method,
        k=k,
        n=n,
        rationale=rationale,
    )
