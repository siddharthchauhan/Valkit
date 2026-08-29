"""Tests for the binomial bounds and sample sizing.

The values asserted here are not this implementation's own output recorded as
a baseline. They are published or independently derivable figures, which is
the only kind of test that can detect a systematically wrong bound: an
acceptance engine that is confidently wrong in the same way every time would
pass any self-consistency check.
"""

from __future__ import annotations

import math

import pytest

from valkit.errors import AcceptanceError
from valkit.stats.proportions import (
    additional_passes_needed,
    agresti_coull_interval,
    clopper_pearson_interval,
    clopper_pearson_lower,
    jeffreys_lower,
    max_failures_for_n,
    min_n_with_failures,
    min_n_zero_failures,
    non_inferiority,
    student_t_mean_lower,
    wald_lower,
    wilson_interval,
    wilson_lower,
)


class TestPublishedIntervals:
    """Anchors against values that appear in the statistical literature."""

    def test_wilson_30_of_100(self):
        interval = wilson_interval(30, 100, 0.95)
        assert round(interval.lower, 3) == 0.219
        assert round(interval.upper, 3) == 0.396

    def test_clopper_pearson_30_of_100(self):
        interval = clopper_pearson_interval(30, 100, 0.95)
        assert round(interval.lower, 4) == 0.2124
        assert round(interval.upper, 4) == 0.3998

    def test_clopper_pearson_is_wider_than_wilson(self):
        """The exact interval is conservative; that is its defining property."""
        exact = clopper_pearson_interval(30, 100, 0.95)
        score = wilson_interval(30, 100, 0.95)
        assert exact.lower < score.lower
        assert exact.upper > score.upper

    def test_worked_oq_example_from_the_design(self):
        """176/180 with a Wilson 95% lower bound clears a 0.95 target."""
        bound = wilson_lower(176, 180, 0.95)
        assert round(bound, 3) == 0.951
        assert bound >= 0.95

    def test_same_case_fails_under_the_exact_method(self):
        """The choice of method is consequential and must be made in the plan.

        176/180 clears 0.95 under Wilson and misses it under Clopper-Pearson.
        A specification that leaves the method unstated leaves the verdict
        unstated.
        """
        assert wilson_lower(176, 180, 0.95) >= 0.95
        assert clopper_pearson_lower(176, 180, 0.95) < 0.95


class TestOneSidedVersusTwoSided:
    """The distinction that is easiest to get wrong and worst to get wrong."""

    def test_one_sided_lower_exceeds_two_sided_lower(self):
        one = clopper_pearson_interval(30, 100, 0.95, two_sided=False).lower
        two = clopper_pearson_interval(30, 100, 0.95, two_sided=True).lower
        assert one > two

    def test_one_sided_95_equals_two_sided_90_lower_limit(self):
        """Putting 5% in one tail is the same as splitting 10% between two."""
        one_sided = clopper_pearson_lower(30, 100, 0.95)
        two_sided = clopper_pearson_interval(30, 100, 0.90, two_sided=True).lower
        assert one_sided == pytest.approx(two_sided, abs=1e-12)

    def test_wilson_one_sided_matches_the_same_identity(self):
        one_sided = wilson_lower(30, 100, 0.95)
        two_sided = wilson_interval(30, 100, 0.90, two_sided=True).lower
        assert one_sided == pytest.approx(two_sided, abs=1e-12)

    def test_one_sided_interval_upper_limit_is_one(self):
        assert wilson_interval(30, 100, 0.95, two_sided=False).upper == 1.0
        assert clopper_pearson_interval(30, 100, 0.95, two_sided=False).upper == 1.0


class TestDegenerateCases:
    def test_zero_successes_gives_zero_lower_limit(self):
        assert clopper_pearson_interval(0, 10).lower == 0.0
        assert clopper_pearson_lower(0, 10) == 0.0
        assert wilson_interval(0, 10).lower == 0.0
        assert jeffreys_lower(0, 10) == 0.0

    def test_all_successes_gives_unit_upper_limit(self):
        assert clopper_pearson_interval(10, 10).upper == 1.0
        assert wilson_interval(10, 10).upper == 1.0

    def test_all_successes_does_not_give_a_unit_lower_bound(self):
        """A perfect run is not proof of perfection; the bound must stay below 1."""
        bound = clopper_pearson_lower(59, 59, 0.95)
        assert 0.94 < bound < 1.0

    def test_wald_wrongly_claims_certainty_at_k_equals_n(self):
        """Documented as the reason Wald must not support an acceptance claim."""
        assert wald_lower(10, 10, 0.95) == 1.0
        assert clopper_pearson_lower(10, 10, 0.95) < 1.0

    def test_single_sample(self):
        assert 0.0 < clopper_pearson_lower(1, 1, 0.95) < 1.0
        assert wilson_interval(1, 1, 0.95).lower > 0.0

    @pytest.mark.parametrize(
        "k, n, confidence",
        [(-1, 10, 0.95), (11, 10, 0.95), (5, 0, 0.95), (5, -1, 0.95), (5, 10, 0.0), (5, 10, 1.0)],
    )
    def test_invalid_arguments_raise(self, k, n, confidence):
        with pytest.raises(AcceptanceError):
            clopper_pearson_lower(k, n, confidence)

    def test_zero_samples_message_names_the_real_problem(self):
        with pytest.raises(AcceptanceError, match="at least one scored sample"):
            wilson_lower(0, 0, 0.95)

    def test_non_integer_counts_rejected(self):
        with pytest.raises(AcceptanceError):
            clopper_pearson_lower(3.0, 10, 0.95)


class TestMonotonicity:
    """Properties that must hold for any correct bound, checked by sweep."""

    def test_bound_increases_with_successes(self):
        bounds = [clopper_pearson_lower(k, 50, 0.95) for k in range(51)]
        assert bounds == sorted(bounds)

    def test_bound_decreases_with_confidence(self):
        bounds = [clopper_pearson_lower(45, 50, c) for c in (0.80, 0.90, 0.95, 0.99)]
        assert bounds == sorted(bounds, reverse=True)

    def test_bound_is_below_the_point_estimate(self):
        for k, n in [(1, 10), (30, 100), (176, 180), (59, 59)]:
            assert clopper_pearson_lower(k, n, 0.95) <= k / n
            assert wilson_lower(k, n, 0.95) <= k / n

    def test_more_evidence_at_the_same_rate_tightens_the_bound(self):
        assert clopper_pearson_lower(9, 10, 0.95) < clopper_pearson_lower(90, 100, 0.95)
        assert clopper_pearson_lower(90, 100, 0.95) < clopper_pearson_lower(900, 1000, 0.95)


class TestSampleSizing:
    @pytest.mark.parametrize("target, expected", [(0.95, 59), (0.98, 149), (0.99, 299)])
    def test_zero_failure_table(self, target, expected):
        """The table quoted in the product's own documentation."""
        assert min_n_zero_failures(target, 0.95) == expected

    def test_zero_failure_rule_matches_its_closed_form(self):
        for target in (0.90, 0.95, 0.98, 0.99, 0.999):
            expected = math.ceil(math.log(0.05) / math.log(target))
            assert min_n_zero_failures(target, 0.95) == expected

    @pytest.mark.parametrize("failures, expected", [(0, 59), (1, 93), (2, 124)])
    def test_allowing_failures_raises_the_requirement(self, failures, expected):
        assert min_n_with_failures(0.95, 0.95, failures) == expected

    @pytest.mark.parametrize("target", [0.95, 0.98, 0.99])
    @pytest.mark.parametrize("failures", [0, 1, 2, 3])
    def test_returned_n_is_the_smallest_that_works(self, target, failures):
        """The boundary check: n satisfies the claim and n-1 does not."""
        n = min_n_with_failures(target, 0.95, failures)
        assert clopper_pearson_lower(n - failures, n, 0.95) >= target
        assert clopper_pearson_lower(n - 1 - failures, n - 1, 0.95) < target

    def test_zero_failure_size_agrees_with_the_exact_search(self):
        for target in (0.90, 0.95, 0.98, 0.99):
            assert min_n_zero_failures(target, 0.95) == min_n_with_failures(target, 0.95, 0)

    def test_max_failures_for_n(self):
        assert max_failures_for_n(180, 0.95, 0.95) == 3
        assert clopper_pearson_lower(180 - 3, 180, 0.95) >= 0.95
        assert clopper_pearson_lower(180 - 4, 180, 0.95) < 0.95

    def test_under_powered_golden_set_is_reported_as_such(self):
        """Ten cases cannot demonstrate 0.99 even if all ten pass."""
        assert max_failures_for_n(10, 0.99, 0.95) == -1

    @pytest.mark.parametrize("target", [0.0, 1.0, -0.1, 1.5])
    def test_impossible_targets_rejected(self, target):
        with pytest.raises(AcceptanceError):
            min_n_zero_failures(target, 0.95)

    def test_negative_failures_rejected(self):
        with pytest.raises(AcceptanceError):
            min_n_with_failures(0.95, 0.95, -1)


class TestAdditionalPasses:
    def test_already_passing_needs_nothing(self):
        assert additional_passes_needed(59, 59, 0.95, 0.95) == 0

    def test_shortfall_is_actually_sufficient(self):
        k, n, target = 90, 100, 0.95
        extra = additional_passes_needed(k, n, target, 0.95)
        assert extra > 0
        assert clopper_pearson_lower(k + extra, n + extra, 0.95) >= target
        assert clopper_pearson_lower(k + extra - 1, n + extra - 1, 0.95) < target


class TestMeanBound:
    def test_known_t_bound(self):
        """Hand-checkable: mean 10, sd 2, n 25, t(0.95, 24) = 1.7109."""
        values = [10.0] * 25
        assert student_t_mean_lower(values, 0.95) == 10.0

    def test_bound_below_mean_with_spread(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        mean = 3.0
        bound = student_t_mean_lower(values, 0.95)
        assert bound < mean
        # sd = 1.5811, se = 0.7071, t(0.95, 4) = 2.13185
        assert bound == pytest.approx(3.0 - 2.131846786 * 0.70710678, abs=1e-6)

    def test_single_observation_supports_no_bound(self):
        with pytest.raises(AcceptanceError, match="at least 2 observations"):
            student_t_mean_lower([1.0], 0.95)


class TestNonInferiority:
    def test_passes_within_margin(self):
        result = non_inferiority(176, 180, baseline=0.97, margin=0.05, confidence=0.95)
        assert result.passed
        assert result.threshold == pytest.approx(0.92)
        assert "Non-inferiority demonstrated" in result.rationale

    def test_fails_outside_margin(self):
        result = non_inferiority(80, 100, baseline=0.97, margin=0.02, confidence=0.95)
        assert not result.passed
        assert "NOT demonstrated" in result.rationale

    def test_zero_margin_is_strict_superiority_of_the_bound(self):
        result = non_inferiority(176, 180, baseline=0.95, margin=0.0, confidence=0.95)
        assert result.threshold == 0.95
        assert result.passed is (result.lower_bound >= 0.95)

    def test_invalid_margin_rejected(self):
        with pytest.raises(AcceptanceError):
            non_inferiority(50, 100, baseline=0.9, margin=-0.1)


class TestAgrestiCoull:
    def test_interval_brackets_the_point_estimate_away_from_the_edges(self):
        interval = agresti_coull_interval(30, 100, 0.95)
        assert interval.lower < 0.30 < interval.upper

    def test_behaves_at_zero_successes(self):
        interval = agresti_coull_interval(0, 20, 0.95)
        assert interval.lower == 0.0
        assert interval.upper > 0.0


class TestIntervalRecord:
    def test_unpacks_as_a_pair(self):
        low, high = wilson_interval(30, 100, 0.95)
        assert round(low, 3) == 0.219
        assert round(high, 3) == 0.396

    def test_carries_its_provenance(self):
        interval = clopper_pearson_interval(30, 100, 0.95)
        assert interval.method == "clopper_pearson"
        assert interval.confidence == 0.95
        assert interval.k == 30 and interval.n == 100
        assert interval.point_estimate == 0.30
