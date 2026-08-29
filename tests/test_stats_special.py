"""Tests for the numerical primitives.

Two kinds of check are used. Closed-form identities (``I_x(a, 1) = x^a`` and
the like) hold exactly and need no external authority. Everything else is
compared against ``mpmath`` at 40 decimal digits, which is an independent
implementation by a different author using different algorithms — the only
comparison that can catch a shared misconception. ``mpmath`` is a test-only
cross-check and is not a runtime dependency; the suite skips those checks
rather than failing when it is absent.
"""

from __future__ import annotations

import math

import pytest

from valkit.stats.special import (
    _lanczos_log_gamma,
    inverse_regularized_incomplete_beta,
    log_beta,
    log_gamma,
    normal_cdf,
    normal_quantile,
    regularized_incomplete_beta,
    student_t_quantile,
)

def _mp():
    """Return mpmath at 40 digits, or skip the test if it is unavailable."""
    mp = pytest.importorskip("mpmath")
    mp.mp.dps = 40
    return mp


class TestLogGamma:
    def test_known_values(self):
        assert log_gamma(1.0) == pytest.approx(0.0, abs=1e-15)
        assert log_gamma(2.0) == pytest.approx(0.0, abs=1e-15)
        assert log_gamma(0.5) == pytest.approx(math.log(math.sqrt(math.pi)), abs=1e-14)
        # Gamma(6) = 120
        assert log_gamma(6.0) == pytest.approx(math.log(120.0), abs=1e-13)

    def test_lanczos_series_agrees_with_the_library(self):
        """The documented algorithm reproduces the value actually returned."""
        for x in (0.1, 0.5, 1.0, 2.0, 5.0, 10.5, 100.0, 1000.0):
            assert _lanczos_log_gamma(x) == pytest.approx(math.lgamma(x), rel=1e-13)

    def test_non_positive_rejected(self):
        for x in (0.0, -1.0):
            with pytest.raises(ValueError):
                log_gamma(x)

    def test_log_beta_symmetry(self):
        assert log_beta(3.0, 7.0) == pytest.approx(log_beta(7.0, 3.0), abs=1e-15)

    def test_log_beta_known_value(self):
        # B(1, 1) = 1
        assert log_beta(1.0, 1.0) == pytest.approx(0.0, abs=1e-15)
        # B(2, 3) = 1/12
        assert log_beta(2.0, 3.0) == pytest.approx(math.log(1 / 12), abs=1e-14)


class TestIncompleteBeta:
    def test_closed_form_identities(self):
        """I_x(a, 1) = x^a and I_x(1, b) = 1 - (1-x)^b, exactly."""
        for x in (0.1, 0.5, 0.9):
            for a in (1.0, 2.0, 3.0, 7.0):
                assert regularized_incomplete_beta(a, 1.0, x) == pytest.approx(x**a, abs=1e-14)
            for b in (1.0, 2.0, 3.0, 7.0):
                assert regularized_incomplete_beta(1.0, b, x) == pytest.approx(
                    1.0 - (1.0 - x) ** b, abs=1e-14
                )

    def test_symmetry_relation(self):
        """I_x(a, b) = 1 - I_{1-x}(b, a) across the continued-fraction boundary."""
        for a, b, x in [(2.0, 5.0, 0.3), (30.0, 71.0, 0.25), (0.5, 0.5, 0.8), (100.0, 3.0, 0.97)]:
            left = regularized_incomplete_beta(a, b, x)
            right = 1.0 - regularized_incomplete_beta(b, a, 1.0 - x)
            assert left == pytest.approx(right, abs=1e-13)

    def test_boundaries(self):
        assert regularized_incomplete_beta(3.0, 4.0, 0.0) == 0.0
        assert regularized_incomplete_beta(3.0, 4.0, 1.0) == 1.0

    def test_monotone_increasing_in_x(self):
        values = [regularized_incomplete_beta(3.0, 5.0, x / 100) for x in range(101)]
        assert values == sorted(values)

    def test_evaluated_on_both_sides_of_the_convergence_boundary(self):
        """The threshold is x = (a+1)/(a+b+2); check either side and at it."""
        a, b = 4.0, 6.0
        threshold = (a + 1.0) / (a + b + 2.0)
        below = regularized_incomplete_beta(a, b, threshold - 1e-6)
        at = regularized_incomplete_beta(a, b, threshold)
        above = regularized_incomplete_beta(a, b, threshold + 1e-6)
        assert below < at < above

    @pytest.mark.parametrize(
        "a, b, x",
        [(5, 0.5, 0.05), (30, 71, 0.2124), (0.5, 0.5, 0.3), (100, 200, 0.33), (1, 1, 0.5),
         (200.0, 1.0, 0.999), (2.5, 3.5, 0.42)],
    )
    def test_against_arbitrary_precision(self, a, b, x):
        mp = _mp()
        expected = float(mp.betainc(a, b, 0, x, regularized=True))
        assert regularized_incomplete_beta(a, b, x) == pytest.approx(expected, abs=1e-13)

    def test_invalid_arguments(self):
        with pytest.raises(ValueError):
            regularized_incomplete_beta(0.0, 1.0, 0.5)
        with pytest.raises(ValueError):
            regularized_incomplete_beta(1.0, 1.0, 1.5)


class TestInverseIncompleteBeta:
    def test_round_trip(self):
        """The inverse must actually invert, across skewed shapes."""
        for a, b in [(1.0, 1.0), (30.0, 71.0), (0.5, 0.5), (176.0, 5.0), (2.0, 200.0)]:
            for p in (0.001, 0.025, 0.05, 0.5, 0.95, 0.975, 0.999):
                x = inverse_regularized_incomplete_beta(a, b, p)
                assert regularized_incomplete_beta(a, b, x) == pytest.approx(p, abs=1e-12)

    def test_closed_form(self):
        """I_x(a, 1) = x^a inverts to p^(1/a)."""
        for a in (2.0, 3.0, 5.0):
            for p in (0.05, 0.5, 0.95):
                assert inverse_regularized_incomplete_beta(a, 1.0, p) == pytest.approx(
                    p ** (1.0 / a), abs=1e-12
                )

    def test_boundaries(self):
        assert inverse_regularized_incomplete_beta(3.0, 4.0, 0.0) == 0.0
        assert inverse_regularized_incomplete_beta(3.0, 4.0, 1.0) == 1.0

    def test_extreme_shape_parameters(self):
        """A 0.99 acceptance target puts the solver on a very skewed Beta."""
        x = inverse_regularized_incomplete_beta(299.0, 1.0, 0.05)
        assert 0.0 < x < 1.0
        assert regularized_incomplete_beta(299.0, 1.0, x) == pytest.approx(0.05, abs=1e-12)


class TestNormal:
    def test_cdf_known_values(self):
        assert normal_cdf(0.0) == pytest.approx(0.5, abs=1e-15)
        assert normal_cdf(1.96) == pytest.approx(0.9750021048517795, abs=1e-14)
        assert normal_cdf(-1.96) == pytest.approx(0.024997895148220435, abs=1e-14)

    def test_quantile_known_values(self):
        assert normal_quantile(0.975) == pytest.approx(1.959963984540054, abs=1e-12)
        assert normal_quantile(0.95) == pytest.approx(1.6448536269514722, abs=1e-12)
        assert normal_quantile(0.99) == pytest.approx(2.3263478740408408, abs=1e-12)
        assert normal_quantile(0.5) == pytest.approx(0.0, abs=1e-12)

    def test_quantile_inverts_cdf(self):
        for p in (1e-12, 1e-6, 0.001, 0.02, 0.3, 0.5, 0.7, 0.98, 0.999, 1 - 1e-12):
            assert normal_cdf(normal_quantile(p)) == pytest.approx(p, rel=1e-9)

    def test_quantile_symmetry(self):
        for p in (0.01, 0.1, 0.3):
            assert normal_quantile(p) == pytest.approx(-normal_quantile(1.0 - p), abs=1e-12)

    def test_far_tail_accuracy(self):
        """The Acklam approximation is weakest in the tails; the refinement fixes it."""
        mp = _mp()
        for p in (1e-10, 1e-8, 1e-4):
            expected = float(mp.sqrt(2) * mp.erfinv(2 * mp.mpf(p) - 1))
            assert normal_quantile(p) == pytest.approx(expected, abs=1e-11)

    def test_out_of_domain(self):
        for p in (0.0, 1.0, -0.1, 1.1):
            with pytest.raises(ValueError):
                normal_quantile(p)


class TestStudentT:
    def test_known_table_values(self):
        """Quantiles computed independently at 40 digits by bisecting the exact
        t CDF in ``mpmath``. Printed t tables carry four or five significant
        figures, which is not enough to detect an error in the eleventh, so the
        references here were derived rather than transcribed."""
        assert student_t_quantile(0.975, 10) == pytest.approx(2.2281388519862742, abs=1e-12)
        assert student_t_quantile(0.95, 1) == pytest.approx(6.3137515146750374, abs=1e-12)
        assert student_t_quantile(0.95, 4) == pytest.approx(2.1318467863266495, abs=1e-12)
        assert student_t_quantile(0.95, 24) == pytest.approx(1.7108820799094280, abs=1e-12)
        assert student_t_quantile(0.975, 100) == pytest.approx(1.9839715185235519, abs=1e-12)

    def test_median_is_zero(self):
        assert student_t_quantile(0.5, 10) == 0.0

    def test_symmetry(self):
        assert student_t_quantile(0.05, 10) == pytest.approx(
            -student_t_quantile(0.95, 10), abs=1e-12
        )

    def test_converges_to_normal_as_df_grows(self):
        assert student_t_quantile(0.975, 1_000_000) == pytest.approx(
            normal_quantile(0.975), abs=1e-5
        )

    def test_heavier_tails_than_normal(self):
        assert student_t_quantile(0.975, 5) > normal_quantile(0.975)

    def test_invalid_arguments(self):
        with pytest.raises(ValueError):
            student_t_quantile(0.0, 10)
        with pytest.raises(ValueError):
            student_t_quantile(0.5, 0)
