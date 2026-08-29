"""Tests for statistical process control and drift monitoring.

Each control rule is tripped by a hand-built series designed to trip that rule
and no other. False positives are checked as carefully as false negatives: an
alerting system that cries wolf is one people learn to ignore, which is a worse
outcome than no alerting at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from valkit.drift.monitor import (
    DriftMonitor,
    InMemoryMonitoringStore,
    JsonMonitoringStore,
    cron_matches,
    next_due,
    parse_cron,
)
from valkit.drift.spc import (
    D2_CONSTANT,
    control_limits,
    evaluate_rules,
    nelson,
    p_chart_limits,
    threshold_rule,
    western_electric,
)
from valkit.errors import ValKitError
from valkit.models import (
    AlertSeverity,
    DriftPoint,
    MonitoringSpec,
    SpcRule,
)
from valkit.testing import make_run
from valkit.util import FrozenClock

# A steady baseline: mean 0.97, small consistent variation.
STEADY = [0.97, 0.98, 0.97, 0.96, 0.97, 0.98, 0.96, 0.97, 0.98, 0.97]


def limits_for(values=STEADY):
    return control_limits(values, bounded=True)


class TestControlLimits:
    def test_centre_is_the_mean(self):
        limits = limits_for()
        assert limits.center == pytest.approx(sum(STEADY) / len(STEADY))

    def test_sigma_uses_the_moving_range_estimator(self):
        """Hand-computed: mean moving range divided by 1.128."""
        values = [1.0, 2.0, 3.0, 2.0]
        ranges = [1.0, 1.0, 1.0]
        expected = (sum(ranges) / len(ranges)) / D2_CONSTANT
        assert control_limits(values).sigma == pytest.approx(expected)

    def test_moving_range_resists_a_shift_that_stdev_would_absorb(self):
        """The reason an individuals chart uses the moving range at all."""
        import statistics

        shifted = [0.97] * 10 + [0.80] * 10
        limits = control_limits(shifted)
        assert limits.sigma < statistics.stdev(shifted)

    def test_bounded_limits_are_clamped_to_the_unit_interval(self):
        limits = control_limits([0.99, 1.0, 0.98, 1.0], bounded=True)
        assert limits.upper <= 1.0
        assert limits.lower >= 0.0

    def test_unbounded_limits_may_exceed_one(self):
        limits = control_limits([0.99, 1.0, 0.98, 1.0], bounded=False)
        assert limits.upper > 1.0

    def test_window_limits_the_history_used(self):
        long_series = [0.5] * 20 + STEADY
        assert control_limits(long_series, window=len(STEADY)).center == pytest.approx(
            sum(STEADY) / len(STEADY)
        )

    def test_a_single_observation_is_refused(self):
        with pytest.raises(ValKitError, match="at least 2 observations"):
            control_limits([0.97])

    def test_zone_and_sigma_line(self):
        limits = limits_for()
        assert limits.sigma_line(3.0) == pytest.approx(limits.center + 3 * limits.sigma)
        assert limits.zone(limits.center) == 0


class TestWesternElectric:
    def test_rule_1_one_point_beyond_three_sigma(self):
        limits = limits_for()
        violations = western_electric([*STEADY, 0.80], limits)
        assert [v.rule for v in violations if v.index == len(STEADY)] == ["WE1"]
        assert violations[0].severity is AlertSeverity.CRITICAL

    def test_rule_1_describes_the_finding_in_plain_terms(self):
        violations = western_electric([*STEADY, 0.80], limits_for())
        assert "beyond three sigma" in violations[0].description
        assert "ordinary variation" in violations[0].description

    def test_rule_2_two_of_three_beyond_two_sigma(self):
        limits = limits_for()
        two_sigma_below = limits.sigma_line(-2.2)
        series = [*STEADY, two_sigma_below, limits.center, two_sigma_below]
        rules = {v.rule for v in western_electric(series, limits)}
        assert "WE2" in rules

    def test_rule_3_four_of_five_beyond_one_sigma(self):
        limits = limits_for()
        one_sigma_below = limits.sigma_line(-1.3)
        series = [*STEADY, one_sigma_below, one_sigma_below, limits.center, one_sigma_below,
                  one_sigma_below]
        rules = {v.rule for v in western_electric(series, limits)}
        assert "WE3" in rules

    def test_rule_4_eight_consecutive_points_on_one_side(self):
        limits = limits_for()
        above = limits.center + 0.2 * limits.sigma
        rules = {v.rule for v in western_electric([above] * 8, limits)}
        assert "WE4" in rules

    def test_rule_4_run_length_is_configurable(self):
        limits = limits_for()
        above = limits.center + 0.2 * limits.sigma
        assert not [v for v in western_electric([above] * 8, limits, run_length=9)
                    if v.rule == "WE4"]
        assert [v for v in western_electric([above] * 9, limits, run_length=9)
                if v.rule == "WE4"]

    def test_a_steady_series_trips_nothing(self):
        """False positives matter as much as false negatives."""
        assert western_electric(STEADY, limits_for()) == []

    def test_a_flat_baseline_treats_any_change_as_a_signal(self):
        limits = control_limits([0.97] * 6)
        assert limits.sigma == 0.0
        violations = western_electric([0.97, 0.97, 0.90], limits)
        assert len(violations) == 1
        assert "no historical variation" in violations[0].description


class TestNelson:
    def test_rule_1(self):
        assert any(v.rule == "N1" for v in nelson([*STEADY, 0.80], limits_for()))

    def test_rule_2_nine_on_one_side(self):
        limits = limits_for()
        above = limits.center + 0.2 * limits.sigma
        assert any(v.rule == "N2" for v in nelson([above] * 9, limits))

    def test_rule_3_six_point_trend(self):
        limits = limits_for()
        trend = [limits.center + i * 0.1 * limits.sigma for i in range(6)]
        assert any(v.rule == "N3" for v in nelson(trend, limits))

    def test_rule_3_does_not_fire_on_five(self):
        limits = limits_for()
        trend = [limits.center + i * 0.1 * limits.sigma for i in range(5)]
        assert not any(v.rule == "N3" for v in nelson(trend, limits))

    def test_rule_4_alternating(self):
        limits = limits_for()
        alternating = [
            limits.center + (0.3 if i % 2 else -0.3) * limits.sigma for i in range(14)
        ]
        assert any(v.rule == "N4" for v in nelson(alternating, limits))

    def test_rule_7_stratification(self):
        limits = limits_for()
        hugging = [limits.center + 0.1 * limits.sigma] * 15
        assert any(v.rule == "N7" for v in nelson(hugging, limits))

    def test_rule_8_mixture(self):
        limits = limits_for()
        mixture = [
            limits.center + (1.5 if i % 2 else -1.5) * limits.sigma for i in range(8)
        ]
        assert any(v.rule == "N8" for v in nelson(mixture, limits))

    def test_a_steady_series_trips_nothing(self):
        assert nelson(STEADY, limits_for()) == []


class TestThreshold:
    def test_below_the_target_is_critical(self):
        violations = threshold_rule([0.97, 0.94], lower=0.95)
        assert len(violations) == 1
        assert violations[0].severity is AlertSeverity.CRITICAL
        assert "no longer supported" in violations[0].description

    def test_at_the_target_is_not_a_violation(self):
        assert threshold_rule([0.95], lower=0.95) == []

    def test_evaluate_rules_combines_the_set_with_the_target(self):
        limits = limits_for()
        violations = evaluate_rules(
            [*STEADY, 0.90], limits, SpcRule.WESTERN_ELECTRIC, target=0.95
        )
        rules = {v.rule for v in violations}
        assert "THRESHOLD_LOW" in rules

    def test_threshold_rule_set_uses_only_the_target(self):
        violations = evaluate_rules([*STEADY, 0.80], limits_for(), SpcRule.THRESHOLD, target=0.95)
        assert {v.rule for v in violations} == {"THRESHOLD_LOW"}


class TestPChart:
    def test_limits_narrow_as_the_sample_grows(self):
        limits = p_chart_limits([19, 190], [20, 200])
        assert (limits[1].upper - limits[1].lower) < (limits[0].upper - limits[0].lower)

    def test_centre_is_the_pooled_proportion(self):
        limits = p_chart_limits([19, 190], [20, 200])
        assert limits[0].center == pytest.approx(209 / 220)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValKitError, match="same length"):
            p_chart_limits([1, 2], [10])

    def test_zero_sample_size_rejected(self):
        with pytest.raises(ValKitError, match="positive sample size"):
            p_chart_limits([0], [0])


class TestCron:
    @pytest.mark.parametrize(
        "expression, moment, expected",
        [
            ("0 6 * * 1", dt.datetime(2026, 4, 6, 6, 0), True),    # a Monday
            ("0 6 * * 1", dt.datetime(2026, 4, 7, 6, 0), False),   # a Tuesday
            ("0 6 * * *", dt.datetime(2026, 4, 7, 6, 0), True),
            ("30 * * * *", dt.datetime(2026, 4, 7, 13, 30), True),
            ("30 * * * *", dt.datetime(2026, 4, 7, 13, 31), False),
            ("0 */4 * * *", dt.datetime(2026, 4, 7, 8, 0), True),
            ("0 */4 * * *", dt.datetime(2026, 4, 7, 9, 0), False),
            ("0 6 1-7 * *", dt.datetime(2026, 4, 3, 6, 0), True),
            ("0 6 1-7 * *", dt.datetime(2026, 4, 8, 6, 0), False),
            ("0 6 * jan *", dt.datetime(2026, 1, 8, 6, 0), True),
            ("0 6 * jan *", dt.datetime(2026, 2, 8, 6, 0), False),
            ("0 6 * * mon", dt.datetime(2026, 4, 6, 6, 0), True),
            ("0 6 * * 0", dt.datetime(2026, 4, 5, 6, 0), True),    # Sunday as 0
            ("0 6 * * 7", dt.datetime(2026, 4, 5, 6, 0), True),    # Sunday as 7
        ],
    )
    def test_matching(self, expression, moment, expected):
        assert cron_matches(expression, moment) is expected

    def test_day_of_month_and_day_of_week_are_a_union(self):
        """The semantics most implementations get wrong."""
        expression = "0 6 1 * 1"  # the 1st, OR any Monday
        assert cron_matches(expression, dt.datetime(2026, 4, 1, 6, 0))   # a Wednesday 1st
        assert cron_matches(expression, dt.datetime(2026, 4, 6, 6, 0))   # a Monday, not 1st
        assert not cron_matches(expression, dt.datetime(2026, 4, 7, 6, 0))

    def test_unrestricted_day_fields_do_not_apply_the_union(self):
        assert cron_matches("0 6 * * *", dt.datetime(2026, 4, 7, 6, 0))

    def test_step_within_a_range(self):
        assert cron_matches("0 8-18/2 * * *", dt.datetime(2026, 4, 7, 10, 0))
        assert not cron_matches("0 8-18/2 * * *", dt.datetime(2026, 4, 7, 11, 0))

    def test_lists(self):
        assert cron_matches("0 6,18 * * *", dt.datetime(2026, 4, 7, 18, 0))
        assert not cron_matches("0 6,18 * * *", dt.datetime(2026, 4, 7, 12, 0))

    @pytest.mark.parametrize(
        "expression",
        ["0 6 * *", "0 6 * * * *", "0 99 * * *", "0 6 32 * *", "0 6 * 13 *",
         "0 6 * * 9", "x 6 * * *", "0 6-2 * * *", "0 */0 * * *"],
    )
    def test_invalid_expressions_are_rejected(self, expression):
        with pytest.raises(ValKitError):
            parse_cron(expression)

    def test_next_due(self):
        moment = dt.datetime(2026, 4, 7, 12, 0)  # a Tuesday
        following = next_due("0 6 * * 1", moment)
        assert following == dt.datetime(2026, 4, 13, 6, 0)


class TestMonitoringStores:
    def test_in_memory_round_trip(self):
        store = InMemoryMonitoringStore()
        store.append(DriftPoint(agent_id="a", metric="m", observed_at="t", value=0.9))
        assert len(store.series("a", "m")) == 1
        assert store.metrics("a") == ["m"]

    def test_json_store_persists(self, workdir):
        path = workdir / "monitoring.jsonl"
        first = JsonMonitoringStore(path)
        first.append(DriftPoint(agent_id="a", metric="m", observed_at="t", value=0.9))

        second = JsonMonitoringStore(path)
        assert [p.value for p in second.series("a", "m")] == [0.9]

    def test_json_store_is_append_only_in_form(self, workdir):
        path = workdir / "monitoring.jsonl"
        store = JsonMonitoringStore(path)
        for index in range(3):
            store.append(
                DriftPoint(agent_id="a", metric="m", observed_at=f"t{index}", value=0.9)
            )
        assert len(path.read_text().strip().split("\n")) == 3


class TestDriftMonitor:
    def _monitor(self, clock=None):
        return DriftMonitor(InMemoryMonitoringStore(), clock=clock or FrozenClock(step=1))

    def _seed(self, monitor, values, agent="a", metric="field_accuracy"):
        for index, value in enumerate(values):
            monitor.store.append(
                DriftPoint(
                    agent_id=agent,
                    metric=metric,
                    observed_at=f"2026-01-{index + 1:02d}T06:00:00Z",
                    value=value,
                    n=100,
                )
            )

    def test_recording_a_run_appends_a_point_per_metric(self):
        monitor = self._monitor()
        run = make_run(n=20)
        from valkit.models import BoundMethod, MetricResult, MetricType

        run = run.replace(
            metrics=[
                MetricResult(
                    name="field_accuracy",
                    type=MetricType.PROPORTION,
                    n=20,
                    k=19,
                    point_estimate=0.95,
                    method=BoundMethod.CLOPPER_PEARSON_LOWER,
                    confidence=0.95,
                    passed=True,
                )
            ]
        )
        points = monitor.record(run)
        assert len(points) == 1
        assert points[0].value == 0.95

    def test_no_alert_without_enough_history(self):
        monitor = self._monitor()
        self._seed(monitor, [0.97, 0.50])
        assert monitor.evaluate("a", "field_accuracy", MonitoringSpec()) is None

    def test_an_out_of_control_point_raises_an_alert(self):
        monitor = self._monitor()
        self._seed(monitor, [*STEADY, 0.80])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec())
        assert alert is not None
        assert alert.severity is AlertSeverity.CRITICAL
        assert any(v.rule == "WE1" for v in alert.violations)

    def test_a_steady_series_raises_nothing(self):
        monitor = self._monitor()
        self._seed(monitor, STEADY)
        assert monitor.evaluate("a", "field_accuracy", MonitoringSpec()) is None

    def test_the_point_under_test_is_excluded_from_its_own_limits(self):
        """Otherwise an outlier widens the limits toward itself and escapes."""
        monitor = self._monitor()
        self._seed(monitor, [*STEADY, 0.80])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec())

        limits_without = control_limits(STEADY, bounded=True)
        assert alert.center_line == pytest.approx(limits_without.center)
        assert alert.lower_control_limit == pytest.approx(limits_without.lower)

    def test_a_marginal_point_would_escape_if_it_widened_its_own_limits(self):
        """The case the exclusion actually exists for.

        A gross outlier is caught either way. A marginal one is not: 0.94 sits
        below the limits computed from the history alone, but including it
        inflates the moving range enough to bring the lower limit under it, and
        the point escapes the test meant to catch it.
        """
        marginal = 0.94
        excluded = control_limits(STEADY, bounded=True)
        included = control_limits([*STEADY, marginal], bounded=True)
        assert marginal < excluded.lower, "the history alone would catch it"
        assert marginal > included.lower, "including it would hide it"

        monitor = self._monitor()
        self._seed(monitor, [*STEADY, marginal])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec())
        assert alert is not None
        assert any(v.rule == "WE1" for v in alert.violations)

    def test_falling_below_the_target_is_always_critical(self):
        monitor = self._monitor()
        # A drop small enough that only a weak zone rule fires.
        self._seed(monitor, [*STEADY, 0.949])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec(), target=0.95)
        assert alert is not None
        assert alert.severity is AlertSeverity.CRITICAL
        assert "no longer supported" in alert.message

    def test_an_old_violation_is_not_re_reported(self):
        """A violation is actionable when it happens, not on every later run."""
        monitor = self._monitor()
        self._seed(monitor, [0.80, *STEADY])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec())
        # The historical 0.80 must not appear among the current violations; a
        # rule may still fire on the newest point for other reasons.
        if alert is not None:
            assert all(v.value != 0.80 for v in alert.violations)
            assert all(v.index == len(monitor.store.series("a", "field_accuracy")) - 1
                       for v in alert.violations)

    def test_check_all_covers_every_metric(self):
        monitor = self._monitor()
        self._seed(monitor, [*STEADY, 0.80], metric="field_accuracy")
        self._seed(monitor, STEADY, metric="citation_accuracy")
        alerts = monitor.check_all("a", MonitoringSpec())
        assert [a.metric for a in alerts] == ["field_accuracy"]

    def test_the_alert_renders_for_a_human(self):
        monitor = self._monitor()
        self._seed(monitor, [*STEADY, 0.80])
        alert = monitor.evaluate("a", "field_accuracy", MonitoringSpec())
        rendered = DriftMonitor.render_alert(alert)
        assert "[DRIFT]" in rendered
        assert "WE1" in rendered
        assert "Centre line" in rendered

    def test_audit_events_are_written(self):
        from valkit.audit import AuditTrail

        clock = FrozenClock(step=1)
        audit = AuditTrail(":memory:", clock)
        monitor = DriftMonitor(InMemoryMonitoringStore(), clock=clock, audit=audit)
        self._seed(monitor, [*STEADY, 0.80])
        monitor.evaluate("a", "field_accuracy", MonitoringSpec())
        assert audit.filter(action="monitoring.alert_raised")
        assert audit.verify().ok

    def test_a_change_control_is_opened_when_configured(self):
        from valkit.change import ChangeControlRegister

        clock = FrozenClock(step=1)
        register = ChangeControlRegister(clock=clock)
        monitor = DriftMonitor(
            InMemoryMonitoringStore(), clock=clock, change_register=register
        )
        self._seed(monitor, [*STEADY, 0.80])
        alert = monitor.evaluate(
            "a", "field_accuracy", MonitoringSpec(auto_change_control=True)
        )
        assert alert.change_control_id
        assert register.get(alert.change_control_id).required_scope == ["field_accuracy"]

    def test_no_change_control_when_disabled(self):
        from valkit.change import ChangeControlRegister

        clock = FrozenClock(step=1)
        register = ChangeControlRegister(clock=clock)
        monitor = DriftMonitor(
            InMemoryMonitoringStore(), clock=clock, change_register=register
        )
        self._seed(monitor, [*STEADY, 0.80])
        alert = monitor.evaluate(
            "a", "field_accuracy", MonitoringSpec(auto_change_control=False)
        )
        assert alert.change_control_id is None


class TestSchedule:
    def test_an_agent_with_no_history_is_due(self):
        monitor = DriftMonitor(InMemoryMonitoringStore(), clock=FrozenClock(step=1))
        assert monitor.due("a", MonitoringSpec(schedule="0 6 * * 1"))

    def test_no_schedule_is_never_due(self):
        monitor = DriftMonitor(InMemoryMonitoringStore(), clock=FrozenClock(step=1))
        assert not monitor.due("a", MonitoringSpec(schedule=None))

    def test_a_recent_run_is_not_due(self):
        clock = FrozenClock("2026-04-06T07:00:00Z", step=1)
        monitor = DriftMonitor(InMemoryMonitoringStore(), clock=clock)
        monitor.store.append(
            DriftPoint(
                agent_id="a", metric="m", observed_at="2026-04-06T06:30:00Z", value=0.97
            )
        )
        assert not monitor.due("a", MonitoringSpec(schedule="0 6 * * 1"))

    def test_a_stale_run_is_due(self):
        clock = FrozenClock("2026-04-13T07:00:00Z", step=1)
        monitor = DriftMonitor(InMemoryMonitoringStore(), clock=clock)
        monitor.store.append(
            DriftPoint(
                agent_id="a", metric="m", observed_at="2026-04-06T06:30:00Z", value=0.97
            )
        )
        assert monitor.due("a", MonitoringSpec(schedule="0 6 * * 1"))
