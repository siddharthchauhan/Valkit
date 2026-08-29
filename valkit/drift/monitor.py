"""Drift monitoring: keeping a validated status honest after release.

A validation package is a statement about a system at a moment. For an agent
built on a hosted model, the system can change without anyone on the customer's
side changing anything: a provider updates a model, and behaviour moves. Without
monitoring, validated status decays silently, and the first anyone knows is when
an inspector asks how the claim is still supported.

This module ingests each re-evaluation, plots the metric against control limits
computed from its own history, and raises an alert when a rule trips. The
important detail is which history: the point being tested is excluded from the
limits it is tested against, because otherwise an outlier widens the limits
toward itself and escapes the very test meant to catch it.

Severity is not taken from the rule alone. A control-rule violation says the
process has changed; falling below the acceptance target says the claim in the
signed package is no longer true. The second is always critical, whatever the
chart says.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from ..audit.store import AuditTrail
from ..errors import ValKitError
from ..models import (
    AlertSeverity,
    DriftAlert,
    DriftPoint,
    EvalRun,
    MonitoringSpec,
    SpcRule,
    SpcViolation,
)
from ..util import Clock, SystemClock, parse_utc
from .spc import ControlLimits, control_limits, evaluate_rules

__all__ = [
    "MonitoringStore",
    "JsonMonitoringStore",
    "InMemoryMonitoringStore",
    "DriftMonitor",
    "CronExpression",
    "cron_matches",
    "next_due",
]


# --------------------------------------------------------------------------
# Cron
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CronExpression:
    """A parsed five-field cron expression.

    Written here rather than taken from a library so that the scheduling
    semantics a validation depends on are visible and testable. The rule most
    implementations get wrong is the day-of-month and day-of-week interaction:
    when both are restricted, the standard cron behaviour is a **union**, not an
    intersection — the job runs if either matches. That surprises people, so it
    is implemented explicitly and tested.
    """

    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool
    source: str

    def matches(self, moment) -> bool:
        if moment.minute not in self.minute:
            return False
        if moment.hour not in self.hour:
            return False
        if moment.month not in self.month:
            return False

        # Python's weekday() is Monday=0; cron uses Sunday=0.
        weekday = (moment.weekday() + 1) % 7
        dom_ok = moment.day in self.day_of_month
        dow_ok = weekday in self.day_of_week

        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_DAY_NAMES = {
    name: index
    for index, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"], 0)
}


def _parse_field(
    text: str, low: int, high: int, names: dict[str, int] | None, label: str
) -> tuple[frozenset[int], bool]:
    """Parse one cron field, returning its values and whether it is restricted."""
    restricted = text.strip() != "*"
    values: set[int] = set()

    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            raise ValKitError(f"empty {label} in cron expression")

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ValKitError(f"invalid step {step_text!r} in cron {label}") from None
            if step <= 0:
                raise ValKitError(f"cron {label} step must be positive, got {step}")
            if part in ("", "*"):
                part = f"{low}-{high}"

        if part == "*":
            values.update(range(low, high + 1, step))
            continue

        if "-" in part and not part.startswith("-"):
            start_text, _, end_text = part.partition("-")
            start = _resolve(start_text, names, low, high, label)
            end = _resolve(end_text, names, low, high, label)
            if start > end:
                raise ValKitError(f"cron {label} range {part!r} is inverted")
            values.update(range(start, end + 1, step))
            continue

        single = _resolve(part, names, low, high, label)
        values.update(range(single, high + 1, step) if step > 1 else [single])

    return frozenset(values), restricted


def _resolve(text: str, names: dict[str, int] | None, low: int, high: int, label: str) -> int:
    text = text.strip().lower()
    if names and text in names:
        value = names[text]
    else:
        try:
            value = int(text)
        except ValueError:
            raise ValKitError(f"invalid value {text!r} in cron {label}") from None
    # Cron accepts 7 for Sunday as well as 0.
    if label == "day-of-week" and value == 7:
        value = 0
    if not low <= value <= high:
        raise ValKitError(f"cron {label} value {value} is outside {low}-{high}")
    return value


def parse_cron(expression: str) -> CronExpression:
    """Parse a five-field cron expression."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValKitError(
            f"expected a 5-field cron expression, got {len(fields)}: {expression!r}"
        )
    minute, restricted_minute = _parse_field(fields[0], 0, 59, None, "minute")
    hour, _ = _parse_field(fields[1], 0, 23, None, "hour")
    dom, dom_restricted = _parse_field(fields[2], 1, 31, None, "day-of-month")
    month, _ = _parse_field(fields[3], 1, 12, _MONTH_NAMES, "month")
    dow, dow_restricted = _parse_field(fields[4], 0, 6, _DAY_NAMES, "day-of-week")
    return CronExpression(
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month=month,
        day_of_week=dow,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
        source=expression,
    )


def cron_matches(expression: str, moment) -> bool:
    """Whether a cron expression fires at the given moment."""
    return parse_cron(expression).matches(moment)


def next_due(expression: str, after, *, horizon_days: int = 400):
    """The next firing at or after ``after``, or ``None`` within the horizon."""
    import datetime as _dt

    parsed = parse_cron(expression)
    moment = after.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    limit = after + _dt.timedelta(days=horizon_days)
    while moment <= limit:
        if parsed.matches(moment):
            return moment
        moment += _dt.timedelta(minutes=1)
    return None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@runtime_checkable
class MonitoringStore(Protocol):
    def append(self, point: DriftPoint) -> None: ...

    def series(self, agent_id: str, metric: str) -> list[DriftPoint]: ...

    def metrics(self, agent_id: str) -> list[str]: ...


class InMemoryMonitoringStore:
    """For tests and short-lived processes."""

    def __init__(self) -> None:
        self._points: list[DriftPoint] = []

    def append(self, point: DriftPoint) -> None:
        self._points.append(point)

    def series(self, agent_id: str, metric: str) -> list[DriftPoint]:
        return [p for p in self._points if p.agent_id == agent_id and p.metric == metric]

    def metrics(self, agent_id: str) -> list[str]:
        return sorted({p.metric for p in self._points if p.agent_id == agent_id})


class JsonMonitoringStore:
    """Append-only JSON lines, one file per store.

    Append-only like everything else that records what happened: a monitoring
    history that can be edited is not evidence that the agent stayed within its
    limits.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, point: DriftPoint) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(point.to_json() + "\n")

    def _all(self) -> list[DriftPoint]:
        points: list[DriftPoint] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            points.append(
                DriftPoint(
                    agent_id=raw["agent_id"],
                    metric=raw["metric"],
                    observed_at=raw["observed_at"],
                    value=raw["value"],
                    run_id=raw.get("run_id"),
                    n=raw.get("n"),
                    lower_bound=raw.get("lower_bound"),
                )
            )
        return points

    def series(self, agent_id: str, metric: str) -> list[DriftPoint]:
        return [p for p in self._all() if p.agent_id == agent_id and p.metric == metric]

    def metrics(self, agent_id: str) -> list[str]:
        return sorted({p.metric for p in self._all() if p.agent_id == agent_id})


# --------------------------------------------------------------------------
# The monitor
# --------------------------------------------------------------------------


class DriftMonitor:
    """Ingests re-evaluations and raises alerts when a control rule trips."""

    def __init__(
        self,
        store: MonitoringStore | None = None,
        *,
        clock: Clock | None = None,
        audit: AuditTrail | None = None,
        change_register: Any = None,
    ):
        self.store = store or InMemoryMonitoringStore()
        self._clock = clock or SystemClock()
        self._audit = audit
        self._change_register = change_register
        self._alert_counter = 0

    # -- ingestion ---------------------------------------------------------

    def record(self, run: EvalRun) -> list[DriftPoint]:
        """Append one point per metric from a completed run."""
        points: list[DriftPoint] = []
        for metric in run.metrics:
            point = DriftPoint(
                agent_id=run.agent_id,
                metric=metric.name,
                observed_at=run.finished_at or run.started_at or self._clock.now_iso(),
                value=metric.point_estimate,
                run_id=run.run_id,
                n=metric.n,
                lower_bound=metric.lower_bound,
            )
            self.store.append(point)
            points.append(point)

        if self._audit is not None and points:
            self._audit.append(
                actor="system",
                action="monitoring.recorded",
                entity_type="agent",
                entity_id=run.agent_id,
                payload={"run_id": run.run_id, "metrics": [p.metric for p in points]},
            )
        return points

    # -- evaluation --------------------------------------------------------

    def evaluate(
        self,
        agent_id: str,
        metric: str,
        spec: MonitoringSpec,
        *,
        target: float | None = None,
        channels: Sequence[str] | None = None,
    ) -> DriftAlert | None:
        """Test the most recent point against limits built from its history.

        Returns ``None`` when there is nothing to report, including when there
        is not yet enough history to establish a baseline. Reporting a drift
        alert from two observations would be noise, and an alerting system
        people learn to ignore is worse than none.
        """
        series = self.store.series(agent_id, metric)
        if len(series) < 3:
            return None

        history = series[:-1][-spec.window :]
        latest = series[-1]
        if len(history) < 2:
            return None

        limits = control_limits(
            [p.value for p in history], bounded=True, window=spec.window
        )
        violations = evaluate_rules(
            [p.value for p in series[-spec.window :]],
            limits,
            spec.spc_rule,
            [p.observed_at for p in series[-spec.window :]],
            target=target,
        )

        # Only violations implicating the newest point are actionable now;
        # earlier ones were reported when they occurred.
        latest_index = len(series[-spec.window :]) - 1
        current = [v for v in violations if v.index == latest_index]
        if not current:
            return None

        severity = self._severity(current, latest.value, target)
        self._alert_counter += 1
        alert = DriftAlert(
            alert_id=f"DRIFT-{agent_id}-{metric}-{self._alert_counter:04d}",
            agent_id=agent_id,
            metric=metric,
            raised_at=self._clock.now_iso(),
            severity=severity,
            violations=current,
            center_line=limits.center,
            lower_control_limit=limits.lower,
            upper_control_limit=limits.upper,
            channels=list(channels or spec.alert_channels),
            message=self._message(metric, latest, current, limits, target),
        )

        if spec.auto_change_control and self._change_register is not None:
            from ..models import ChangeTrigger

            change = self._change_register.open(
                agent_id=agent_id,
                agent_version="",
                trigger=ChangeTrigger.DRIFT,
                reason=alert.message,
            )
            self._change_register.assess_impact(change.cc_id, metrics=[metric])
            alert = alert.replace(change_control_id=change.cc_id)

        if self._audit is not None:
            self._audit.append(
                actor="system",
                action="monitoring.alert_raised",
                entity_type="agent",
                entity_id=agent_id,
                payload={
                    "alert_id": alert.alert_id,
                    "metric": metric,
                    "severity": severity.value,
                    "rules": [v.rule for v in current],
                    "value": latest.value,
                    "change_control_id": alert.change_control_id,
                },
            )
        return alert

    def check_all(
        self, agent_id: str, spec: MonitoringSpec, targets: dict[str, float] | None = None
    ) -> list[DriftAlert]:
        """Evaluate every monitored metric for an agent."""
        alerts = []
        for metric in self.store.metrics(agent_id):
            alert = self.evaluate(
                agent_id, metric, spec, target=(targets or {}).get(metric)
            )
            if alert is not None:
                alerts.append(alert)
        return alerts

    @staticmethod
    def _severity(
        violations: Sequence[SpcViolation], value: float, target: float | None
    ) -> AlertSeverity:
        """Falling below the acceptance target is always critical.

        A control rule says the process changed. The target says the claim in
        the signed package is no longer supported, which is a different and
        graver statement.
        """
        if target is not None and value < target:
            return AlertSeverity.CRITICAL
        ranked = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
        return max((v.severity for v in violations), key=lambda s: ranked[s])

    @staticmethod
    def _message(
        metric: str,
        point: DriftPoint,
        violations: Sequence[SpcViolation],
        limits: ControlLimits,
        target: float | None,
    ) -> str:
        rules = ", ".join(sorted({v.rule for v in violations}))
        message = (
            f"{metric} observed {point.value:.4f} on {point.observed_at}; "
            f"control rule(s) {rules} tripped against a centre line of "
            f"{limits.center:.4f} (limits {limits.lower:.4f} to {limits.upper:.4f})."
        )
        if target is not None and point.value < target:
            message += (
                f" The value is below the acceptance target of {target:.4f}: the claim "
                f"made in the validation package is no longer supported."
            )
        return message

    # -- scheduling --------------------------------------------------------

    def due(self, agent_id: str, spec: MonitoringSpec, *, now=None) -> bool:
        """Whether a scheduled re-evaluation is overdue.

        Compares the last recorded observation against the most recent moment
        the schedule should have fired.
        """
        if not spec.schedule:
            return False
        moment = now or self._clock.now()

        latest: str | None = None
        for metric in self.store.metrics(agent_id):
            series = self.store.series(agent_id, metric)
            if series:
                candidate = series[-1].observed_at
                if latest is None or candidate > latest:
                    latest = candidate
        if latest is None:
            return True

        previous = _previous_firing(spec.schedule, moment)
        return previous is not None and parse_utc(latest) < previous

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def render_alert(alert: DriftAlert) -> str:
        """The human-readable alert sent to a channel or pasted into a ticket."""
        lines = [
            f"[DRIFT] {alert.agent_id} — {alert.metric} — {alert.severity.value.upper()}",
            f"Raised {alert.raised_at}",
            "",
            alert.message,
            "",
            "Control rules tripped:",
        ]
        for violation in alert.violations:
            lines.append(f"  {violation.rule}: {violation.description}")
        lines.extend(
            [
                "",
                f"Centre line {alert.center_line:.4f}, control limits "
                f"{alert.lower_control_limit:.4f} to {alert.upper_control_limit:.4f}.",
            ]
        )
        if alert.change_control_id:
            lines.append(
                f"Change control {alert.change_control_id} has been opened. The agent's "
                f"validated status is under review until the required re-evaluation "
                f"passes."
            )
        else:
            lines.append(
                "No change control was opened automatically. Review is required to decide "
                "whether the validated status still holds."
            )
        return "\n".join(lines)


def _previous_firing(expression: str, before, *, horizon_days: int = 400):
    """The most recent firing at or before ``before``."""
    import datetime as _dt

    parsed = parse_cron(expression)
    moment = before.replace(second=0, microsecond=0)
    limit = before - _dt.timedelta(days=horizon_days)
    while moment >= limit:
        if parsed.matches(moment):
            return moment
        moment -= _dt.timedelta(minutes=1)
    return None
