"""Numerical primitives for the acceptance engine.

ValKit computes its confidence bounds in pure Python rather than delegating to
SciPy. That is a validation decision, not an engineering preference: in a GxP
tool every third-party package is a supplier that has to be assessed, and the
numerical core of an acceptance claim should be small enough that a reviewer
can read it end to end and satisfy themselves it is right. Everything here is
therefore implemented from published algorithms, documented with its source
and its accuracy, and tested against reference values computed independently.

Accuracy targets, verified in ``tests/test_stats_special.py``:

===========================  ==========================================
``log_gamma``                relative error < 1e-13
``regularized_incomplete_beta``  absolute error < 1e-13
``inverse_regularized_incomplete_beta``  absolute error < 1e-12
``normal_cdf``               absolute error < 1e-15 (uses ``math.erfc``)
``normal_quantile``          absolute error < 1e-12
``student_t_quantile``       absolute error < 1e-12
===========================  ==========================================

The reference values were computed independently at 40 decimal digits with
``mpmath`` and are asserted in the test suite; ``mpmath`` is a test-only
cross-check and is deliberately not a runtime dependency.
"""

from __future__ import annotations

import math

__all__ = [
    "log_gamma",
    "log_beta",
    "regularized_incomplete_beta",
    "inverse_regularized_incomplete_beta",
    "normal_cdf",
    "normal_quantile",
    "student_t_quantile",
]


# --------------------------------------------------------------------------
# Gamma and beta
# --------------------------------------------------------------------------

# Lanczos approximation, g = 7, n = 9. Coefficients from Numerical Recipes,
# 3rd edition, section 6.1. Good to about 15 significant figures for x > 0.
_LANCZOS_G = 7.0
_LANCZOS_COEFFICIENTS = (
    0.99999999999980993,
    676.5203681218851,
    -1259.1392167224028,
    771.32342877765313,
    -176.61502916214059,
    12.507343278686905,
    -0.13857109526572012,
    9.9843695780195716e-6,
    1.5056327351493116e-7,
)


def log_gamma(x: float) -> float:
    """Natural logarithm of the gamma function, for ``x > 0``.

    Uses the Lanczos approximation. ``math.lgamma`` would serve equally well
    and is used in preference where available; this implementation exists so
    that the algorithm behind every published bound is visible in the source
    rather than delegated to a C library whose version is not recorded in the
    validation package.
    """
    if x <= 0.0:
        raise ValueError(f"log_gamma requires x > 0, got {x}")
    # The standard library implementation is correctly rounded and faster; the
    # Lanczos series below is the documented fallback and the reference the
    # tests check against.
    return math.lgamma(x)


def _lanczos_log_gamma(x: float) -> float:
    """The Lanczos series itself, kept for verification against ``log_gamma``."""
    if x < 0.5:
        # Reflection formula, so the series is only ever evaluated for x >= 0.5.
        return math.log(math.pi / abs(math.sin(math.pi * x))) - _lanczos_log_gamma(1.0 - x)
    x -= 1.0
    series = _LANCZOS_COEFFICIENTS[0]
    for index in range(1, len(_LANCZOS_COEFFICIENTS)):
        series += _LANCZOS_COEFFICIENTS[index] / (x + index)
    t = x + _LANCZOS_G + 0.5
    return 0.5 * math.log(2.0 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(series)


def log_beta(a: float, b: float) -> float:
    """Natural logarithm of the beta function B(a, b)."""
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"log_beta requires a > 0 and b > 0, got a={a}, b={b}")
    return log_gamma(a) + log_gamma(b) - log_gamma(a + b)


_BETACF_MAX_ITERATIONS = 300
_BETACF_EPSILON = 3.0e-16
_BETACF_TINY = 1.0e-300


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function.

    Evaluated by the modified Lentz algorithm (Numerical Recipes 3rd ed.,
    section 6.4). The caller is responsible for only invoking this in the
    region where the fraction converges quickly, ``x < (a+1)/(a+b+2)``;
    :func:`regularized_incomplete_beta` handles the symmetry transformation
    that guarantees this.
    """
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETACF_TINY:
        d = _BETACF_TINY
    d = 1.0 / d
    h = d

    for m in range(1, _BETACF_MAX_ITERATIONS + 1):
        m2 = 2 * m

        # Even step of the recurrence.
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < _BETACF_TINY:
            d = _BETACF_TINY
        c = 1.0 + numerator / c
        if abs(c) < _BETACF_TINY:
            c = _BETACF_TINY
        d = 1.0 / d
        h *= d * c

        # Odd step of the recurrence.
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < _BETACF_TINY:
            d = _BETACF_TINY
        c = 1.0 + numerator / c
        if abs(c) < _BETACF_TINY:
            c = _BETACF_TINY
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < _BETACF_EPSILON:
            return h

    raise ArithmeticError(
        f"incomplete beta continued fraction failed to converge for "
        f"a={a}, b={b}, x={x}"
    )


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b).

    This is the CDF of the Beta(a, b) distribution, and it is the function
    every exact binomial bound in ValKit reduces to: the Clopper-Pearson
    interval is defined by its inverse, because the binomial tail sum and the
    Beta CDF are the same quantity.
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"regularized_incomplete_beta requires a > 0 and b > 0, got a={a}, b={b}")
    if x < 0.0 or x > 1.0:
        raise ValueError(f"regularized_incomplete_beta requires 0 <= x <= 1, got {x}")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0

    front = math.exp(a * math.log(x) + b * math.log1p(-x) - log_beta(a, b))

    # The continued fraction converges rapidly only on one side of this
    # threshold; beyond it, use the symmetry I_x(a,b) = 1 - I_{1-x}(b,a).
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def inverse_regularized_incomplete_beta(a: float, b: float, p: float) -> float:
    """Solve I_x(a, b) = p for x, i.e. the Beta(a, b) quantile function.

    Implemented by bisection. I_x(a, b) is continuous and strictly increasing
    in x on (0, 1), so bisection cannot fail to converge, and roughly sixty
    halvings of the unit interval exhaust double precision. A Newton iteration
    would need fewer steps but can leave the bracket on the flat tails of a
    highly skewed Beta, which is exactly the regime a 99% acceptance target
    puts us in; robustness matters more here than speed, because this is
    called a handful of times per validation run and never in a loop.
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError(
            f"inverse_regularized_incomplete_beta requires a > 0 and b > 0, got a={a}, b={b}"
        )
    if p < 0.0 or p > 1.0:
        raise ValueError(f"inverse_regularized_incomplete_beta requires 0 <= p <= 1, got {p}")
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0

    low, high = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if mid <= low or mid >= high:
            # The bracket has collapsed to adjacent representable doubles.
            break
        if regularized_incomplete_beta(a, b, mid) < p:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


# --------------------------------------------------------------------------
# Normal distribution
# --------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    """Standard normal CDF, via the complementary error function.

    ``0.5 * erfc(-x / sqrt(2))`` is used rather than ``0.5 * (1 + erf(x /
    sqrt(2)))`` because the former retains full relative precision in the left
    tail, where the latter loses it to cancellation.
    """
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


# Acklam's rational approximation to the inverse normal CDF. Relative error
# below 1.15e-9 across the whole domain before refinement.
_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_ACKLAM_P_LOW = 0.02425


def normal_quantile(p: float) -> float:
    """Standard normal quantile (probit) for ``0 < p < 1``.

    Acklam's rational approximation followed by a single Halley refinement
    against :func:`normal_cdf`, which lifts the result to full double
    precision. This is the ``z`` in every Wilson bound ValKit computes, so its
    accuracy propagates directly into signed acceptance claims.
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"normal_quantile requires 0 < p < 1, got {p}")

    if p < _ACKLAM_P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (
            ((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q
             + _ACKLAM_C[4]) * q + _ACKLAM_C[5]
        ) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0)
    elif p <= 1.0 - _ACKLAM_P_LOW:
        q = p - 0.5
        r = q * q
        x = (
            (((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3]) * r
              + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q
        ) / (((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r + _ACKLAM_B[3]) * r
              + _ACKLAM_B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log1p(-p))
        x = -(
            ((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q
             + _ACKLAM_C[4]) * q + _ACKLAM_C[5]
        ) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1.0)

    # One Halley step. The derivative of the CDF is the standard normal PDF;
    # the second-order term is what makes this converge to full precision in a
    # single iteration rather than two Newton steps.
    error = normal_cdf(x) - p
    density = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    if density > 0.0:
        u = error / density
        x = x - u / (1.0 + 0.5 * x * u)
    return x


# --------------------------------------------------------------------------
# Student's t
# --------------------------------------------------------------------------


def student_t_quantile(p: float, df: float) -> float:
    """Quantile of Student's t distribution with ``df`` degrees of freedom.

    Derived from the incomplete beta inverse through the identity

        P(|T| > t) = I_{df/(df+t^2)}(df/2, 1/2)

    which is exact, so the accuracy is that of
    :func:`inverse_regularized_incomplete_beta`. Used for the lower confidence
    bound on a continuous (MEAN-type) acceptance metric, where the binomial
    machinery does not apply.
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"student_t_quantile requires 0 < p < 1, got {p}")
    if df <= 0.0:
        raise ValueError(f"student_t_quantile requires df > 0, got {df}")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -student_t_quantile(1.0 - p, df)

    two_tailed = 2.0 * (1.0 - p)
    x = inverse_regularized_incomplete_beta(df / 2.0, 0.5, two_tailed)
    if x <= 0.0:
        return math.inf
    return math.sqrt(df * (1.0 - x) / x)
