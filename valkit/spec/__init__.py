"""Specification loading, risk assessment and validation-package derivation.

``valkit.yaml`` is the contract between an engineering team and their quality
function. This package reads it (:mod:`loader`), decides how much evidence the
declared use warrants (:mod:`risk`), and derives the requirements, risks and
test cases that the rest of the package hangs off (:mod:`derive`).
"""

from __future__ import annotations

from .derive import (
    DerivedBundle,
    derive_all,
    derive_requirements,
    derive_risks,
    derive_tests,
)
from .loader import (
    SUPPORTED_API_VERSIONS,
    SUPPORTED_KINDS,
    SpecLoadResult,
    dump_spec,
    load_spec,
    load_spec_from_string,
    load_spec_result,
    parse_spec,
)
from .risk import RISK_MATRIX, RequiredRigor, RiskAssessment, assess_risk, matrix_rationale

__all__ = [
    "SpecLoadResult",
    "parse_spec",
    "load_spec",
    "load_spec_from_string",
    "load_spec_result",
    "dump_spec",
    "SUPPORTED_API_VERSIONS",
    "SUPPORTED_KINDS",
    "RiskAssessment",
    "RequiredRigor",
    "assess_risk",
    "matrix_rationale",
    "RISK_MATRIX",
    "DerivedBundle",
    "derive_requirements",
    "derive_risks",
    "derive_tests",
    "derive_all",
]
