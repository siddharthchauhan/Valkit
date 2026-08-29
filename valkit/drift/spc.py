"""Statistical process control for monitored metrics.

Validation is not a one-time event for a system whose behaviour can change
without anything on your side changing. A provider can update a model, a prompt
can be edited, the distribution of real inputs can shift. Control charts are the
established way to distinguish ordinary variation from a real change, and they
are what turns "we re-run the evals weekly" into a decision rule.

Two implementation choices carry the weight.

*Sigma is estimated from the moving range, not the sample standard deviation.*
A process that has already shifted inflates its own standard deviation, which
widens the limits and hides the very shift being looked for. The average moving
range divided by 1.128 — the expected range of two consecutive observations from
a normal distribution — estimates short-term variation only, so a sustained
shift shows up as points outside the limits rather than as wider limits. This is
the standard individuals-chart estimator and the reason individuals charts work
at all.

*The point under test is excluded from the limits it is tested against.*
Including it lets an outlier pull the limits toward itself and escape detection,
the more so the smaller the window.

A note on applying an individuals chart to a proportion. Re-evaluation runs
often differ in size, and a proportion from 200 cases is less variable than one
from 40; an individuals chart ignores that. It is a reasonable approximation
when n is roughly constant, which scheduled re-evaluation against a fixed golden
set usually makes true. Where n varies materially, :func:`p_chart_limits`
computes per-point limits from the binomial variance and should be preferred.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from ..errors import ValKitError
from ..models import AlertSeverity, SpcRule, SpcViolation

__all__ = [
    "ControlLimits",
    "control_limits",
    "p_chart_limits",
    "western_electric",
    "nelson",
    "threshold_rule",
    "evaluate_rules",
    "D2_CONSTANT",
]

# E[range of two consecutive observations] / sigma, for a normal distribution.
D2_CONSTANT = 1.128


@dataclass(frozen=True)
class ControlLimits:
    """Centre line and control limits for a series."""

    center: float
    sigma: float
    upper: float
    lower: float
    n_points: int
    bounded: bool = False
    method: str = "moving_range"

    def zone(self, value: float) -> int:
        """How many sigma from the centre, signed. Zero when sigma is zero."""
        if self.sigma <= 0.0:
            return 0
        return int(abs(value - self.center) / self.sigma) * (1 if value >= self.center else -1)

    def sigma_line(self, multiple: float) -> float:
        return self.center + multiple * self.sigma


def control_limits(
    values: Sequence[float],
    *,
    sigma_multiple: float = 3.0,
    bounded: bool = False,
    window: int | None = None,
) -> ControlLimits:
    """Individuals-chart limits from the moving range.

    ``bounded`` clamps the limits to [0, 1], which is right for a proportion:
    a lower control limit of -0.02 is not wrong so much as meaningless, and it
    would make rule 1 unable to fire.
    """
    series = list(values)[-window:] if window else list(values)
    if len(series) < 2:
        raise ValKitError(
            f"control limits need at least 2 observations, got {len(series)}. "
            "A baseline cannot be established from a single point."
        )

    center = sum(series) / len(series)
    moving_ranges = [abs(series[i] - series[i - 1]) for i in range(1, len(series))]
    average_range = sum(moving_ranges) / len(moving_ranges)
    sigma = average_range / D2_CONSTANT

    upper = center + sigma_multiple * sigma
    lower = center - sigma_multiple * sigma
    if bounded:
        upper = min(1.0, upper)
        lower = max(0.0, lower)

    return ControlLimits(
        center=center,
        sigma=sigma,
        upper=upper,
        lower=lower,
        n_points=len(series),
        bounded=bounded,
        method="moving_range",
    )


def p_chart_limits(
    passes: Sequence[int], totals: Sequence[int], *, sigma_multiple: float = 3.0
) -> list[ControlLimits]:
    """Per-point limits for a proportion whose sample size varies.

    The correct treatment when re-evaluation runs differ in size: the limits
    narrow as n grows, so a small run is not flagged for variation that its
    size fully explains.
    """
    if len(passes) != len(totals):
        raise ValKitError(
            f"passes and totals must be the same length, got {len(passes)} and {len(totals)}"
        )
    if not passes:
        raise ValKitError("a p-chart needs at least one observation")
    if any(n <= 0 for n in totals):
        raise ValKitError("every observation must have a positive sample size")

    grand = sum(passes) / sum(totals)
    limits: list[ControlLimits] = []
    for n in totals:
        sigma = math.sqrt(grand * (1.0 - grand) / n)
        limits.append(
            ControlLimits(
                center=grand,
                sigma=sigma,
                upper=min(1.0, grand + sigma_multiple * sigma),
                lower=max(0.0, grand - sigma_multiple * sigma),
                n_points=len(totals),
                bounded=True,
                method="p_chart",
            )
        )
    return limits


def _stamp(timestamps: Sequence[str] | None, index: int) -> str:
    if timestamps and index < len(timestamps):
        return timestamps[index]
    return ""


def western_electric(
    values: Sequence[float],
    limits: ControlLimits,
    timestamps: Sequence[str] | None = None,
    *,
    run_length: int = 8,
) -> list[SpcViolation]:
    """The four Western Electric zone rules.

    1. One point beyond three sigma.
    2. Two of three consecutive points beyond two sigma on the same side.
    3. Four of five consecutive points beyond one sigma on the same side.
    4. ``run_length`` consecutive points on the same side of the centre line.

    Rule 4's length is eight in the original Western Electric handbook and nine
    in several later treatments including Nelson's. Eight is the default here;
    the difference is a convention, and the validation plan should record which
    was used rather than leave a reader to guess.
    """
    series = list(values)
    if limits.sigma <= 0.0:
        # A perfectly flat baseline gives zero sigma. Only rule 1 can be
        # meaningfully assessed, against the limits themselves.
        return _flat_series_violations(series, limits, timestamps, SpcRule.WESTERN_ELECTRIC)

    violations: list[SpcViolation] = []

    for index, value in enumerate(series):
        if value > limits.upper or value < limits.lower:
            side = "above" if value > limits.upper else "below"
            violations.append(
                SpcViolation(
                    rule="WE1",
                    rule_set=SpcRule.WESTERN_ELECTRIC,
                    index=index,
                    observed_at=_stamp(timestamps, index),
                    value=value,
                    description=(
                        f"One point beyond three sigma: {value:.4f} is {side} the control "
                        f"limit of {(limits.upper if side == 'above' else limits.lower):.4f}. "
                        f"A single point this far from the centre is very unlikely to be "
                        f"ordinary variation."
                    ),
                    severity=AlertSeverity.CRITICAL,
                )
            )

    violations.extend(
        _k_of_n_rule(series, limits, timestamps, k=2, n=3, sigma=2.0, rule="WE2",
                     rule_set=SpcRule.WESTERN_ELECTRIC, severity=AlertSeverity.WARNING)
    )
    violations.extend(
        _k_of_n_rule(series, limits, timestamps, k=4, n=5, sigma=1.0, rule="WE3",
                     rule_set=SpcRule.WESTERN_ELECTRIC, severity=AlertSeverity.WARNING)
    )
    violations.extend(
        _run_rule(series, limits, timestamps, length=run_length, rule="WE4",
                  rule_set=SpcRule.WESTERN_ELECTRIC)
    )
    return violations


def nelson(
    values: Sequence[float],
    limits: ControlLimits,
    timestamps: Sequence[str] | None = None,
) -> list[SpcViolation]:
    """All eight Nelson rules.

    Adds to the zone rules the patterns that indicate something other than a
    level shift: a sustained trend, alternation, stratification (points hugging
    the centre line, which usually means the limits were computed from data that
    already contained the variation), and a lack of variation.
    """
    series = list(values)
    if limits.sigma <= 0.0:
        return _flat_series_violations(series, limits, timestamps, SpcRule.NELSON)

    violations: list[SpcViolation] = []

    for index, value in enumerate(series):
        if value > limits.upper or value < limits.lower:
            violations.append(
                SpcViolation(
                    rule="N1",
                    rule_set=SpcRule.NELSON,
                    index=index,
                    observed_at=_stamp(timestamps, index),
                    value=value,
                    description=f"One point beyond three sigma ({value:.4f}).",
                    severity=AlertSeverity.CRITICAL,
                )
            )

    violations.extend(
        _run_rule(series, limits, timestamps, length=9, rule="N2", rule_set=SpcRule.NELSON)
    )

    # N3: six consecutive points increasing or decreasing.
    for start in range(len(series) - 5):
        window = series[start : start + 6]
        if all(b > a for a, b in zip(window, window[1:])) or all(
            b < a for a, b in zip(window, window[1:])
        ):
            direction = "increasing" if window[-1] > window[0] else "decreasing"
            violations.append(
                SpcViolation(
                    rule="N3",
                    rule_set=SpcRule.NELSON,
                    index=start + 5,
                    observed_at=_stamp(timestamps, start + 5),
                    value=window[-1],
                    description=(
                        f"Six consecutive points {direction}. A trend indicates a process "
                        f"drifting rather than a step change."
                    ),
                    severity=AlertSeverity.WARNING,
                )
            )

    # N4: fourteen consecutive points alternating up and down.
    for start in range(len(series) - 13):
        window = series[start : start + 14]
        deltas = [b - a for a, b in zip(window, window[1:])]
        if all(d != 0 for d in deltas) and all(
            (a > 0) != (b > 0) for a, b in zip(deltas, deltas[1:])
        ):
            violations.append(
                SpcViolation(
                    rule="N4",
                    rule_set=SpcRule.NELSON,
                    index=start + 13,
                    observed_at=_stamp(timestamps, start + 13),
                    value=window[-1],
                    description=(
                        "Fourteen consecutive points alternating up and down, which "
                        "suggests two interleaved sources rather than one process."
                    ),
                    severity=AlertSeverity.INFO,
                )
            )

    violations.extend(
        _k_of_n_rule(series, limits, timestamps, k=2, n=3, sigma=2.0, rule="N5",
                     rule_set=SpcRule.NELSON, severity=AlertSeverity.WARNING)
    )
    violations.extend(
        _k_of_n_rule(series, limits, timestamps, k=4, n=5, sigma=1.0, rule="N6",
                     rule_set=SpcRule.NELSON, severity=AlertSeverity.WARNING)
    )

    # N7: fifteen consecutive points within one sigma of the centre.
    for start in range(len(series) - 14):
        window = series[start : start + 15]
        if all(abs(v - limits.center) < limits.sigma for v in window):
            violations.append(
                SpcViolation(
                    rule="N7",
                    rule_set=SpcRule.NELSON,
                    index=start + 14,
                    observed_at=_stamp(timestamps, start + 14),
                    value=window[-1],
                    description=(
                        "Fifteen consecutive points within one sigma of the centre "
                        "(stratification). Usually the limits were computed from data "
                        "containing variation the process no longer has."
                    ),
                    severity=AlertSeverity.INFO,
                )
            )

    # N8: eight consecutive points more than one sigma from the centre, both sides.
    for start in range(len(series) - 7):
        window = series[start : start + 8]
        if all(abs(v - limits.center) > limits.sigma for v in window) and (
            any(v > limits.center for v in window) and any(v < limits.center for v in window)
        ):
            violations.append(
                SpcViolation(
                    rule="N8",
                    rule_set=SpcRule.NELSON,
                    index=start + 7,
                    observed_at=_stamp(timestamps, start + 7),
                    value=window[-1],
                    description=(
                        "Eight consecutive points more than one sigma from the centre on "
                        "both sides, which indicates a mixture of two processes."
                    ),
                    severity=AlertSeverity.WARNING,
                )
            )

    return violations


def _k_of_n_rule(
    series: Sequence[float],
    limits: ControlLimits,
    timestamps: Sequence[str] | None,
    *,
    k: int,
    n: int,
    sigma: float,
    rule: str,
    rule_set: SpcRule,
    severity: AlertSeverity,
) -> list[SpcViolation]:
    """``k`` of ``n`` consecutive points beyond ``sigma`` on the same side."""
    upper = limits.sigma_line(sigma)
    lower = limits.sigma_line(-sigma)
    violations: list[SpcViolation] = []
    for start in range(len(series) - n + 1):
        window = series[start : start + n]
        above = sum(1 for v in window if v > upper)
        below = sum(1 for v in window if v < lower)
        if above >= k or below >= k:
            side = "above" if above >= k else "below"
            violations.append(
                SpcViolation(
                    rule=rule,
                    rule_set=rule_set,
                    index=start + n - 1,
                    observed_at=_stamp(timestamps, start + n - 1),
                    value=window[-1],
                    description=(
                        f"{k} of {n} consecutive points more than {sigma:.0f} sigma {side} "
                        f"the centre line. A cluster on one side indicates a shift that a "
                        f"single-point rule would not yet catch."
                    ),
                    severity=severity,
                )
            )
    return violations


def _run_rule(
    series: Sequence[float],
    limits: ControlLimits,
    timestamps: Sequence[str] | None,
    *,
    length: int,
    rule: str,
    rule_set: SpcRule,
) -> list[SpcViolation]:
    """``length`` consecutive points on the same side of the centre line."""
    violations: list[SpcViolation] = []
    for start in range(len(series) - length + 1):
        window = series[start : start + length]
        if all(v > limits.center for v in window) or all(v < limits.center for v in window):
            side = "above" if window[0] > limits.center else "below"
            violations.append(
                SpcViolation(
                    rule=rule,
                    rule_set=rule_set,
                    index=start + length - 1,
                    observed_at=_stamp(timestamps, start + length - 1),
                    value=window[-1],
                    description=(
                        f"{length} consecutive points {side} the centre line. A run this "
                        f"long is unlikely by chance and indicates the process level has "
                        f"moved."
                    ),
                    severity=AlertSeverity.WARNING,
                )
            )
    return violations


def _flat_series_violations(
    series: Sequence[float],
    limits: ControlLimits,
    timestamps: Sequence[str] | None,
    rule_set: SpcRule,
) -> list[SpcViolation]:
    """Only the beyond-limits rule is meaningful when sigma is zero."""
    return [
        SpcViolation(
            rule="WE1" if rule_set is SpcRule.WESTERN_ELECTRIC else "N1",
            rule_set=rule_set,
            index=index,
            observed_at=_stamp(timestamps, index),
            value=value,
            description=(
                f"Point {value:.4f} differs from an otherwise constant baseline of "
                f"{limits.center:.4f}. With no historical variation, any change is a "
                f"signal."
            ),
            severity=AlertSeverity.CRITICAL,
        )
        for index, value in enumerate(series)
        # Not an equality test: the mean of a constant series is not exactly
        # that constant in floating point, so comparing directly would flag
        # every point on a perfectly flat baseline.
        if abs(value - limits.center) > 1e-9 * max(1.0, abs(limits.center))
    ]


def threshold_rule(
    values: Sequence[float],
    *,
    lower: float | None = None,
    upper: float | None = None,
    timestamps: Sequence[str] | None = None,
) -> list[SpcViolation]:
    """A fixed limit, typically the acceptance target itself.

    Falling below the target the package was signed against is not a
    statistical curiosity: at that moment the validated claim is no longer
    true, whatever the control chart says.
    """
    violations: list[SpcViolation] = []
    for index, value in enumerate(values):
        if lower is not None and value < lower:
            violations.append(
                SpcViolation(
                    rule="THRESHOLD_LOW",
                    rule_set=SpcRule.THRESHOLD,
                    index=index,
                    observed_at=_stamp(timestamps, index),
                    value=value,
                    description=(
                        f"Observed {value:.4f}, below the acceptance target of {lower:.4f}. "
                        f"The claim the validation package makes is no longer supported."
                    ),
                    severity=AlertSeverity.CRITICAL,
                )
            )
        if upper is not None and value > upper:
            violations.append(
                SpcViolation(
                    rule="THRESHOLD_HIGH",
                    rule_set=SpcRule.THRESHOLD,
                    index=index,
                    observed_at=_stamp(timestamps, index),
                    value=value,
                    description=f"Observed {value:.4f}, above the limit of {upper:.4f}.",
                    severity=AlertSeverity.WARNING,
                )
            )
    return violations


def evaluate_rules(
    values: Sequence[float],
    limits: ControlLimits,
    rule_set: SpcRule,
    timestamps: Sequence[str] | None = None,
    *,
    target: float | None = None,
) -> list[SpcViolation]:
    """Apply the configured rule set, plus the acceptance target where given."""
    if rule_set is SpcRule.NELSON:
        violations = nelson(values, limits, timestamps)
    elif rule_set is SpcRule.THRESHOLD:
        violations = []
    else:
        violations = western_electric(values, limits, timestamps)

    if target is not None:
        violations.extend(threshold_rule(values, lower=target, timestamps=timestamps))
    return violations
