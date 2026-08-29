"""Statistical acceptance for non-deterministic agents.

The package is layered so that each level can be reviewed on its own:

``special``
    Numerical primitives — gamma, incomplete beta and its inverse, the normal
    and Student-t quantiles. Pure Python, checked against reference values.
``proportions``
    Binomial confidence bounds and sample sizing built on those primitives.
``agreement``
    Cohen's kappa and its diagnostics, for calibrating an LLM judge.
``acceptance``
    The bridge from per-sample scores to a pass/fail decision on a
    specification's acceptance criteria.

Nothing here depends on anything outside the standard library.
"""

from __future__ import annotations

from .acceptance import (
    PowerCheck,
    check_power,
    evaluate_acceptance,
    evaluate_metric,
    shortfall,
)
from .agreement import (
    AgreementSummary,
    cohen_kappa,
    confusion_matrix,
    kappa_confidence_interval,
    kappa_diagnostics,
    kappa_standard_error,
    percent_agreement,
    summarise_agreement,
    weighted_kappa,
)
from .proportions import (
    ConfidenceInterval,
    NonInferiorityResult,
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
from .special import (
    inverse_regularized_incomplete_beta,
    log_beta,
    log_gamma,
    normal_cdf,
    normal_quantile,
    regularized_incomplete_beta,
    student_t_quantile,
)

__all__ = [
    # special
    "log_gamma",
    "log_beta",
    "regularized_incomplete_beta",
    "inverse_regularized_incomplete_beta",
    "normal_cdf",
    "normal_quantile",
    "student_t_quantile",
    # proportions
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
    "additional_passes_needed",
    "non_inferiority",
    # agreement
    "AgreementSummary",
    "cohen_kappa",
    "weighted_kappa",
    "percent_agreement",
    "confusion_matrix",
    "kappa_standard_error",
    "kappa_confidence_interval",
    "kappa_diagnostics",
    "summarise_agreement",
    # acceptance
    "PowerCheck",
    "check_power",
    "evaluate_metric",
    "evaluate_acceptance",
    "shortfall",
]
