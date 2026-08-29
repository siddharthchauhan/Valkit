"""Continuous monitoring: keeping a validated status honest after release.

A hosted model can change behaviour without anything on the customer's side
changing, so a validated status with no monitoring decays silently. This
package plots each re-evaluation against control limits built from its own
history and raises an alert when a rule trips.
"""

from __future__ import annotations

from .monitor import (
    CronExpression,
    DriftMonitor,
    InMemoryMonitoringStore,
    JsonMonitoringStore,
    MonitoringStore,
    cron_matches,
    next_due,
    parse_cron,
)
from .spc import (
    D2_CONSTANT,
    ControlLimits,
    control_limits,
    evaluate_rules,
    nelson,
    p_chart_limits,
    threshold_rule,
    western_electric,
)

__all__ = [
    "DriftMonitor",
    "MonitoringStore",
    "InMemoryMonitoringStore",
    "JsonMonitoringStore",
    "CronExpression",
    "parse_cron",
    "cron_matches",
    "next_due",
    "ControlLimits",
    "control_limits",
    "p_chart_limits",
    "western_electric",
    "nelson",
    "threshold_rule",
    "evaluate_rules",
    "D2_CONSTANT",
]
