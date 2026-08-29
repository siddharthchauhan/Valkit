"""Tests for the acceptance engine.

The behaviours asserted here are the ones that stop a validation package from
overstating its evidence: a metric that was never evaluated must not read as
passed, errored samples must be visible rather than absorbed, and a metric that
passes overall must not conceal a stratum that failed.
"""

from __future__ import annotations

import pytest

from valkit.errors import AcceptanceError
from valkit.models import (
    AcceptanceSpec,
    BoundMethod,
    MetricSpec,
    MetricType,
    SampleResult,
    Score,
)
from valkit.stats.acceptance import (
    check_power,
    evaluate_acceptance,
    evaluate_metric,
    shortfall,
)
from valkit.testing import make_sample_results


def metric(**overrides) -> MetricSpec:
    defaults = dict(
        name="field_accuracy",
        type=MetricType.PROPORTION,
        target=0.95,
        confidence=0.95,
        method=BoundMethod.CLOPPER_PEARSON_LOWER,
    )
    defaults.update(overrides)
    return MetricSpec(**defaults)


def samples(passes: int, fails: int = 0, scorer: str = "field_accuracy") -> list[SampleResult]:
    out = []
    for index in range(passes + fails):
        passed = index < passes
        out.append(
            SampleResult(
                sample_id=f"S-{index:04d}",
                output="x",
                scores={scorer: Score(value=1.0 if passed else 0.0, passed=passed, scorer=scorer)},
            )
        )
    return out


class TestProportionMetric:
    def test_passing_metric(self):
        result = evaluate_metric(metric(target=0.90), samples(59, 0))
        assert result.passed
        assert result.k == 59 and result.n == 59
        assert result.failures == 0
        assert result.lower_bound is not None and result.lower_bound >= 0.90

    def test_failing_metric(self):
        result = evaluate_metric(metric(target=0.99), samples(90, 10))
        assert not result.passed
        assert result.failures == 10
        assert "FAIL" in result.rationale

    def test_rationale_reads_like_an_oq_line(self):
        result = evaluate_metric(
            metric(name="citation_accuracy", target=0.95, method=BoundMethod.WILSON_LOWER),
            samples(176, 4, scorer="citation_accuracy"),
        )
        assert "k=176/180" in result.rationale
        assert "p-hat=0.9778" in result.rationale
        assert "wilson lower bound = 0.951" in result.rationale.lower()
        assert result.rationale.rstrip().endswith("PASS.")

    def test_failing_sample_ids_are_listed_and_sorted(self):
        result = evaluate_metric(metric(target=0.99), samples(3, 2))
        assert result.failing_sample_ids == ["S-0003", "S-0004"]

    def test_method_is_honoured(self):
        """176/180 clears 0.95 under Wilson and misses it under Clopper-Pearson."""
        data = samples(176, 4)
        assert evaluate_metric(metric(target=0.95, method=BoundMethod.WILSON_LOWER), data).passed
        assert not evaluate_metric(
            metric(target=0.95, method=BoundMethod.CLOPPER_PEARSON_LOWER), data
        ).passed

    def test_bound_method_none_is_flagged_as_unsupported_by_statistics(self):
        result = evaluate_metric(metric(target=0.95, method=BoundMethod.NONE), samples(96, 4))
        assert result.passed
        assert result.lower_bound is None
        assert "does not support a statistical claim" in result.rationale

    def test_shortfall_is_reported_on_failure(self):
        result = evaluate_metric(metric(target=0.95), samples(90, 10))
        assert "further consecutive passing case" in result.rationale

    def test_max_failures_cap_can_fail_a_metric_that_clears_its_bound(self):
        spec = metric(target=0.80, max_failures=2)
        result = evaluate_metric(spec, samples(96, 4))
        assert result.lower_bound >= 0.80
        assert not result.passed
        assert "Failure cap exceeded" in result.rationale

    def test_non_critical_metric_is_marked_as_such(self):
        result = evaluate_metric(metric(critical=False, target=0.99), samples(90, 10))
        assert result.critical is False
        assert not result.passed


class TestErrorHandling:
    def test_errored_samples_are_excluded_but_counted(self):
        data = samples(50, 0) + [
            SampleResult(sample_id="S-9999", output="", scores={}, error="provider timeout")
        ]
        result = evaluate_metric(metric(target=0.90), data)
        assert result.n == 50
        assert result.errors == 1
        assert "excluded from the denominator" in result.rationale

    def test_all_samples_errored_fails_rather_than_dividing_by_zero(self):
        data = [
            SampleResult(sample_id=f"S-{i}", output="", scores={}, error="boom") for i in range(5)
        ]
        # The scorer never produced a score anywhere, which is the misconfiguration case.
        with pytest.raises(AcceptanceError, match="never evaluated"):
            evaluate_metric(metric(), data)

    def test_all_scored_samples_errored_fails_with_a_reason(self):
        """One sample carries the scorer, so the metric is configured; all error."""
        data = [
            SampleResult(
                sample_id="S-0000",
                output="",
                scores={"field_accuracy": Score(value=1.0, passed=True, scorer="field_accuracy")},
                error="timeout",
            )
        ]
        result = evaluate_metric(metric(), data)
        assert not result.passed
        assert result.n == 0
        assert "No scorable samples" in result.rationale

    def test_unknown_scorer_raises_rather_than_passing_silently(self):
        with pytest.raises(AcceptanceError, match="never evaluated"):
            evaluate_metric(metric(scorer="does_not_exist"), samples(10))

    def test_missing_target_raises(self):
        with pytest.raises(AcceptanceError, match="no target"):
            evaluate_metric(metric(target=None), samples(10))

    def test_no_samples_at_all_fails(self):
        result = evaluate_metric(metric(), [])
        assert not result.passed
        assert result.n == 0


class TestStrata:
    def test_stratum_breakdown_is_produced(self):
        data = []
        for index in range(20):
            stratum = "DM" if index % 2 else "AE"
            passed = not (stratum == "AE" and index < 8)
            data.append(
                SampleResult(
                    sample_id=f"S-{index:04d}",
                    output="x",
                    stratum=stratum,
                    scores={
                        "field_accuracy": Score(
                            value=1.0 if passed else 0.0, passed=passed, scorer="field_accuracy"
                        )
                    },
                )
            )
        result = evaluate_metric(metric(target=0.5, strata=["stratum"]), data)
        by_value = {s.value: s for s in result.strata}
        assert set(by_value) == {"AE", "DM"}
        assert by_value["DM"].k == by_value["DM"].n
        assert by_value["AE"].k < by_value["AE"].n

    def test_strata_counts_sum_to_the_overall_total(self):
        data = samples(10, 2)
        for index, sample in enumerate(data):
            sample.stratum = "A" if index < 6 else "B"
        result = evaluate_metric(metric(target=0.5, strata=["stratum"]), data)
        assert sum(s.n for s in result.strata) == result.n
        assert sum(s.k for s in result.strata) == result.k

    def test_metadata_key_stratification(self):
        data = samples(6)
        for index, sample in enumerate(data):
            sample.metadata["form"] = "DM" if index < 3 else "AE"
        result = evaluate_metric(metric(target=0.5, strata=["form"]), data)
        assert {s.value for s in result.strata} == {"AE", "DM"}
        assert all(s.key == "form" for s in result.strata)

    def test_no_strata_requested_produces_none(self):
        assert evaluate_metric(metric(), samples(10)).strata == []


class TestMeanMetric:
    def test_mean_metric_uses_a_t_bound(self):
        data = [
            SampleResult(
                sample_id=f"S-{i}",
                output="",
                scores={"quality": Score(value=v, passed=v >= 0.8, scorer="quality")},
            )
            for i, v in enumerate([0.9, 0.95, 0.85, 0.92, 0.88, 0.91])
        ]
        spec = metric(name="quality", type=MetricType.MEAN, target=0.80)
        result = evaluate_metric(spec, data)
        assert result.method is BoundMethod.STUDENT_T_LOWER
        assert result.passed
        assert result.lower_bound < result.point_estimate

    def test_mean_metric_needs_two_observations(self):
        data = [
            SampleResult(
                sample_id="S-0",
                output="",
                scores={"quality": Score(value=0.9, passed=True, scorer="quality")},
            )
        ]
        spec = metric(name="quality", type=MetricType.MEAN, target=0.8)
        result = evaluate_metric(spec, data)
        assert not result.passed
        assert "at least 2 scorable samples" in result.rationale


class TestCountMetric:
    def test_count_within_maximum_passes(self):
        data = [
            SampleResult(
                sample_id=f"S-{i}",
                output="",
                scores={"p1_defects": Score(value=0.0, passed=True, scorer="p1_defects")},
            )
            for i in range(10)
        ]
        spec = metric(name="p1_defects", type=MetricType.COUNT, target=None, max_count=0)
        result = evaluate_metric(spec, data)
        assert result.passed
        assert result.k == 0

    def test_count_over_maximum_fails_and_names_the_samples(self):
        data = []
        for i in range(10):
            occurred = i < 3
            data.append(
                SampleResult(
                    sample_id=f"S-{i:04d}",
                    output="",
                    scores={
                        "p1_defects": Score(
                            value=1.0 if occurred else 0.0, passed=True, scorer="p1_defects"
                        )
                    },
                )
            )
        spec = metric(name="p1_defects", type=MetricType.COUNT, target=None, max_count=0)
        result = evaluate_metric(spec, data)
        assert not result.passed
        assert result.k == 3
        assert result.failing_sample_ids == ["S-0000", "S-0001", "S-0002"]

    def test_count_metric_requires_a_maximum(self):
        data = [
            SampleResult(
                sample_id="S-0",
                output="",
                scores={"c": Score(value=1.0, passed=True, scorer="c")},
            )
        ]
        with pytest.raises(AcceptanceError, match="no max_count"):
            evaluate_metric(metric(name="c", type=MetricType.COUNT, max_count=None), data)


class TestNonInferiorityMetric:
    def test_non_inferiority_metric(self):
        spec = metric(
            target=None, method=BoundMethod.NON_INFERIORITY, baseline=0.97, margin=0.05
        )
        result = evaluate_metric(spec, samples(176, 4))
        assert result.passed
        assert result.target == pytest.approx(0.92)
        assert "Non-inferiority demonstrated" in result.rationale

    def test_missing_baseline_raises(self):
        spec = metric(target=None, method=BoundMethod.NON_INFERIORITY, margin=0.05)
        with pytest.raises(AcceptanceError, match="baseline"):
            evaluate_metric(spec, samples(10))


class TestPowerCheck:
    def test_adequate_golden_set(self):
        check = check_power(metric(target=0.95, confidence=0.95), available=100)
        assert check.adequate
        assert check.required_zero_failures == 59

    def test_under_powered_golden_set_is_named_as_such(self):
        check = check_power(metric(target=0.99, confidence=0.95), available=60)
        assert not check.adequate
        assert check.required_zero_failures == 299
        assert "UNDER-POWERED" in check.message
        assert "even if every one passes" in check.message

    def test_boundary_is_inclusive(self):
        assert check_power(metric(target=0.95), available=59).adequate
        assert not check_power(metric(target=0.95), available=58).adequate


class TestShortfall:
    def test_returns_a_sufficient_number(self):
        spec = metric(target=0.95)
        needed = shortfall(90, 100, spec)
        assert needed is not None and needed > 0

    def test_zero_when_already_met(self):
        assert shortfall(59, 59, metric(target=0.95)) == 0

    def test_none_when_no_bound_applies(self):
        assert shortfall(90, 100, metric(method=BoundMethod.NONE)) is None


class TestEvaluateAcceptance:
    def test_evaluates_every_metric(self):
        spec = AcceptanceSpec(
            metrics=[
                metric(name="a", scorer="field_accuracy", target=0.90),
                metric(name="b", scorer="field_accuracy", target=0.999),
            ]
        )
        results = evaluate_acceptance(spec, samples(96, 4))
        assert [r.name for r in results] == ["a", "b"]
        assert results[0].passed and not results[1].passed

    def test_uses_the_shared_factories(self):
        results = evaluate_acceptance(
            AcceptanceSpec(metrics=[metric(target=0.90)]),
            make_sample_results(100, failures=5),
        )
        assert results[0].n == 100 and results[0].k == 95
