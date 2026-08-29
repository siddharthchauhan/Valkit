"""Turning sample scores into an acceptance decision.

This is the seam between evaluation and validation. Everything upstream
produces per-sample scores; everything downstream — the OQ report, the
credibility assessment, the validation summary — states a conclusion. This
module is where the conclusion is drawn, and it is deliberately the only place
it is drawn, so that a reviewer auditing "how did you decide this passed?" has
exactly one function to read.

Two behaviours here exist specifically to prevent a validation package from
making a claim it cannot support:

*A metric with nothing to measure fails.* When every sample errored, or the
named scorer never ran, the metric does not quietly return a bound of zero or
divide by zero; it fails with a rationale saying why. An acceptance criterion
that was never actually evaluated must never read as passed.

*Errored samples are excluded from the denominator but counted and reported.*
Silently treating a provider timeout as a failure would understate the agent;
silently dropping it would overstate the evidence base. Both numbers appear in
the result, and the OQ report renders both.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

from ..errors import AcceptanceError
from ..models import (
    AcceptanceSpec,
    BoundMethod,
    MetricResult,
    MetricSpec,
    MetricType,
    SampleResult,
    StratumResult,
)
from .proportions import (
    additional_passes_needed,
    clopper_pearson_lower,
    jeffreys_lower,
    min_n_with_failures,
    non_inferiority,
    student_t_mean_lower,
    wald_lower,
    wilson_lower,
)

__all__ = [
    "PowerCheck",
    "evaluate_metric",
    "evaluate_acceptance",
    "check_power",
    "shortfall",
]


# Bound methods that operate on a pass count out of a scored total.
_PROPORTION_BOUNDS: dict[BoundMethod, Callable[[int, int, float], float]] = {
    BoundMethod.CLOPPER_PEARSON_LOWER: clopper_pearson_lower,
    BoundMethod.WILSON_LOWER: wilson_lower,
    BoundMethod.JEFFREYS_LOWER: jeffreys_lower,
    BoundMethod.WALD_LOWER: wald_lower,
}


@dataclass(frozen=True)
class PowerCheck:
    """Whether a golden set is large enough to support a metric's target."""

    metric: str
    available: int
    required_zero_failures: int
    adequate: bool
    message: str


def check_power(metric: MetricSpec, available: int) -> PowerCheck:
    """Ask whether ``available`` cases could ever demonstrate the metric's target.

    Worth asking before a run rather than after. If a metric asks for a 0.99
    pass rate at 95% confidence, no result from a 60-case golden set can
    support it — 299 flawless cases are needed — and discovering that after
    executing the battery wastes the run and, worse, invites someone to quietly
    weaken the target to fit the evidence.
    """
    if metric.type not in (MetricType.PROPORTION, MetricType.NUMERIC_TOLERANCE):
        return PowerCheck(metric.name, available, 0, True, "Power check does not apply.")
    if metric.target is None:
        return PowerCheck(metric.name, available, 0, True, "No target set.")

    required = min_n_with_failures(metric.target, metric.confidence, 0)
    adequate = available >= required
    if adequate:
        message = (
            f"{available} cases available; {required} would be the minimum with zero "
            f"failures to demonstrate {metric.target:.4g} at {metric.confidence:.0%} confidence."
        )
    else:
        message = (
            f"UNDER-POWERED: {available} cases available, but demonstrating "
            f"{metric.target:.4g} at {metric.confidence:.0%} confidence needs at least "
            f"{required} cases even if every one passes. No result from this golden set "
            f"can satisfy the criterion as written."
        )
    return PowerCheck(metric.name, available, required, adequate, message)


def _collect(
    metric: MetricSpec, samples: Sequence[SampleResult]
) -> tuple[list[tuple[SampleResult, float, bool]], int]:
    """Partition samples into scored observations and errors.

    Returns the scored triples ``(sample, value, passed)`` and the error count.
    A sample is an error when the provider failed on it or when the metric's
    scorer produced no score for it.
    """
    scorer = metric.scorer_name
    scored: list[tuple[SampleResult, float, bool]] = []
    errors = 0
    seen_scorer = False

    for sample in samples:
        score = sample.scores.get(scorer)
        if score is not None:
            seen_scorer = True
        if sample.error is not None or score is None:
            errors += 1
            continue
        scored.append((sample, float(score.value), bool(score.passed)))

    if not seen_scorer and samples:
        raise AcceptanceError(
            f"metric {metric.name!r} names scorer {scorer!r}, but no sample carries a "
            f"score from it. The metric was never evaluated; it cannot be reported as "
            f"passed or failed."
        )
    return scored, errors


def _stratum_values(sample: SampleResult, key: str) -> str | None:
    """Resolve a stratification key against a sample.

    ``"stratum"`` refers to the dedicated field; any other key is looked up in
    the sample's metadata.
    """
    if key == "stratum":
        return sample.stratum
    value = sample.metadata.get(key)
    return None if value is None else str(value)


def _strata_results(
    metric: MetricSpec,
    scored: Sequence[tuple[SampleResult, float, bool]],
    bound: Callable[[int, int, float], float] | None,
) -> list[StratumResult]:
    """Per-stratum breakdown, so a metric that passes overall but fails on one
    form or one document type cannot hide inside the aggregate."""
    results: list[StratumResult] = []
    for key in metric.strata:
        buckets: dict[str, list[tuple[SampleResult, float, bool]]] = defaultdict(list)
        for entry in scored:
            value = _stratum_values(entry[0], key)
            if value is not None:
                buckets[value].append(entry)
        for value in sorted(buckets):
            group = buckets[value]
            n = len(group)
            k = sum(1 for _, _, passed in group if passed)
            lower = bound(k, n, metric.confidence) if bound and n else None
            results.append(
                StratumResult(
                    key=key,
                    value=value,
                    n=n,
                    k=k,
                    point_estimate=k / n if n else 0.0,
                    lower_bound=lower,
                    passed=None if (lower is None or metric.target is None) else lower >= metric.target,
                )
            )
    return results


def _empty_result(metric: MetricSpec, errors: int, reason: str) -> MetricResult:
    return MetricResult(
        name=metric.name,
        type=metric.type,
        n=0,
        k=0,
        point_estimate=0.0,
        method=metric.method,
        confidence=metric.confidence,
        passed=False,
        target=metric.target,
        lower_bound=None,
        failures=0,
        errors=errors,
        critical=metric.critical,
        rationale=reason,
    )


def evaluate_metric(metric: MetricSpec, samples: Sequence[SampleResult]) -> MetricResult:
    """Evaluate one acceptance criterion against a run's sample results."""
    scored, errors = _collect(metric, samples)

    if metric.type is MetricType.COUNT:
        return _evaluate_count(metric, scored, errors)
    if metric.type is MetricType.MEAN:
        return _evaluate_mean(metric, scored, errors)
    return _evaluate_proportion(metric, scored, errors)


def _evaluate_proportion(
    metric: MetricSpec, scored: Sequence[tuple[SampleResult, float, bool]], errors: int
) -> MetricResult:
    n = len(scored)
    if n == 0:
        return _empty_result(
            metric,
            errors,
            f"No scorable samples for metric {metric.name!r} ({errors} errored). "
            "The criterion was not demonstrated and is recorded as failed.",
        )
    k = sum(1 for _, _, passed in scored if passed)
    failures = n - k
    point = k / n
    failing_ids = sorted(sample.sample_id for sample, _, passed in scored if not passed)

    # A non-inferiority metric derives its threshold from baseline minus
    # margin, so it legitimately carries no explicit target and must be
    # dispatched before the target is required.
    if metric.method is BoundMethod.NON_INFERIORITY:
        return _evaluate_non_inferiority(metric, scored, errors, k, n, failing_ids)

    if metric.target is None:
        raise AcceptanceError(
            f"metric {metric.name!r} is a {metric.type.value} metric but has no target; "
            "an acceptance criterion without a target cannot be evaluated"
        )

    bound_fn = _PROPORTION_BOUNDS.get(metric.method)
    if metric.method is BoundMethod.NONE:
        lower = None
        meets_bound = point >= metric.target
        bound_text = (
            f"point estimate {point:.4f} compared directly to target {metric.target:.4f} "
            "(no confidence bound; this does not support a statistical claim)"
        )
    elif bound_fn is None:
        raise AcceptanceError(
            f"metric {metric.name!r} requests bound method {metric.method.value!r}, "
            f"which is not applicable to a {metric.type.value} metric"
        )
    else:
        lower = bound_fn(k, n, metric.confidence)
        meets_bound = lower >= metric.target
        bound_text = (
            f"one-sided {metric.confidence:.0%} "
            f"{metric.method.value.replace('_lower', '').replace('_', '-')} "
            f"lower bound = {lower:.4f} against target {metric.target:.4f}"
        )

    within_failure_cap = metric.max_failures is None or failures <= metric.max_failures
    passed = meets_bound and within_failure_cap

    rationale = f"k={k}/{n}; p-hat={point:.4f}; {bound_text}. "
    if not within_failure_cap:
        rationale += (
            f"Failure cap exceeded: {failures} failures against a maximum of "
            f"{metric.max_failures}. "
        )
    rationale += "PASS" if passed else "FAIL"
    if errors:
        rationale += (
            f". {errors} sample(s) excluded from the denominator as execution errors "
            f"and reported separately"
        )
    if not passed and lower is not None:
        needed = shortfall(k, n, metric)
        if needed is not None:
            rationale += (
                f". {needed} further consecutive passing case(s) would bring the bound "
                f"to the target"
            )
    rationale += "."

    strata = _strata_results(metric, scored, bound_fn)

    return MetricResult(
        name=metric.name,
        type=metric.type,
        n=n,
        k=k,
        point_estimate=point,
        method=metric.method,
        confidence=metric.confidence,
        passed=passed,
        target=metric.target,
        lower_bound=lower,
        failures=failures,
        errors=errors,
        critical=metric.critical,
        rationale=rationale,
        strata=strata,
        failing_sample_ids=failing_ids,
    )


def _evaluate_non_inferiority(
    metric: MetricSpec,
    scored: Sequence[tuple[SampleResult, float, bool]],
    errors: int,
    k: int,
    n: int,
    failing_ids: list[str],
) -> MetricResult:
    if metric.baseline is None or metric.margin is None:
        raise AcceptanceError(
            f"metric {metric.name!r} uses non-inferiority but is missing "
            f"{'baseline' if metric.baseline is None else 'margin'}"
        )
    result = non_inferiority(k, n, metric.baseline, metric.margin, metric.confidence)
    return MetricResult(
        name=metric.name,
        type=metric.type,
        n=n,
        k=k,
        point_estimate=k / n,
        method=metric.method,
        confidence=metric.confidence,
        passed=result.passed,
        target=result.threshold,
        lower_bound=result.lower_bound,
        failures=n - k,
        errors=errors,
        critical=metric.critical,
        rationale=result.rationale,
        strata=_strata_results(metric, scored, clopper_pearson_lower),
        failing_sample_ids=failing_ids,
    )


def _evaluate_mean(
    metric: MetricSpec, scored: Sequence[tuple[SampleResult, float, bool]], errors: int
) -> MetricResult:
    n = len(scored)
    if n < 2:
        return _empty_result(
            metric,
            errors,
            f"A lower bound on the mean of {metric.name!r} needs at least 2 scorable "
            f"samples, got {n} ({errors} errored). Recorded as failed.",
        )
    if metric.target is None:
        raise AcceptanceError(f"mean metric {metric.name!r} has no target")

    values = [value for _, value, _ in scored]
    mean = sum(values) / n
    lower = student_t_mean_lower(values, metric.confidence)
    passed = lower >= metric.target
    failing_ids = sorted(sample.sample_id for sample, _, passed_flag in scored if not passed_flag)

    return MetricResult(
        name=metric.name,
        type=metric.type,
        n=n,
        k=sum(1 for _, _, flag in scored if flag),
        point_estimate=mean,
        method=BoundMethod.STUDENT_T_LOWER,
        confidence=metric.confidence,
        passed=passed,
        target=metric.target,
        lower_bound=lower,
        failures=n - sum(1 for _, _, flag in scored if flag),
        errors=errors,
        critical=metric.critical,
        rationale=(
            f"n={n}; mean={mean:.4f}; one-sided {metric.confidence:.0%} Student-t lower "
            f"bound = {lower:.4f} against target {metric.target:.4f}. "
            f"{'PASS' if passed else 'FAIL'}."
        ),
        failing_sample_ids=failing_ids,
    )


def _evaluate_count(
    metric: MetricSpec, scored: Sequence[tuple[SampleResult, float, bool]], errors: int
) -> MetricResult:
    """Evaluate a COUNT metric.

    A COUNT scorer marks occurrences rather than passes: any sample whose score
    value is non-zero counts as one occurrence. This is the shape used for
    "zero P1 citation fabrications", where the acceptance criterion is a
    maximum rather than a rate.
    """
    if metric.max_count is None:
        raise AcceptanceError(
            f"count metric {metric.name!r} has no max_count; an acceptance criterion "
            "on a count needs a maximum"
        )
    n = len(scored)
    occurrences = sum(1 for _, value, _ in scored if value != 0.0)
    occurrence_ids = sorted(
        sample.sample_id for sample, value, _ in scored if value != 0.0
    )
    passed = occurrences <= metric.max_count

    return MetricResult(
        name=metric.name,
        type=metric.type,
        n=n,
        k=occurrences,
        point_estimate=float(occurrences),
        method=BoundMethod.NONE,
        confidence=metric.confidence,
        passed=passed,
        target=float(metric.max_count),
        lower_bound=None,
        failures=occurrences,
        errors=errors,
        critical=metric.critical,
        rationale=(
            f"{occurrences} occurrence(s) observed across {n} scored sample(s) against a "
            f"maximum of {metric.max_count}. {'PASS' if passed else 'FAIL'}."
        ),
        failing_sample_ids=occurrence_ids,
    )


def shortfall(k: int, n: int, metric: MetricSpec) -> int | None:
    """Further consecutive passing cases needed to reach the metric's target.

    Returns ``None`` when the target cannot be reached by adding passing cases,
    which happens once the observed failures alone hold the bound down.
    """
    if metric.target is None or metric.method is BoundMethod.NONE:
        return None
    method = "wilson" if metric.method is BoundMethod.WILSON_LOWER else "clopper_pearson"
    try:
        return additional_passes_needed(k, n, metric.target, metric.confidence, method)
    except AcceptanceError:
        return None


def evaluate_acceptance(
    acceptance: AcceptanceSpec, samples: Sequence[SampleResult]
) -> list[MetricResult]:
    """Evaluate every acceptance criterion in a specification."""
    return [evaluate_metric(metric, samples) for metric in acceptance.metrics]
