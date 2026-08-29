"""ValKit — Part 11 validation-as-code for LLM agents in GxP workflows.

ValKit turns an evaluation run of a non-deterministic LLM agent into a signed,
statistically defensible computer-system-validation package: user and
functional requirements, a GAMP 5 risk assessment, an FDA-style credibility
assessment, IQ/OQ/PQ protocols and reports, a requirements-to-test
traceability matrix, immutable evidence, electronic signatures, and drift
monitoring that keeps the agent validated after release.

The public surface is exposed lazily so that importing :mod:`valkit` does not
pull in optional extras (FastAPI, boto3, MCP) that a given deployment may not
have installed.

Typical use::

    from valkit import load_spec, validate_agent

    spec = load_spec("valkit.yaml")
    record = validate_agent(spec)
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

# Public name -> module it lives in. Kept explicit so that a typo surfaces as
# an ImportError at first use rather than a silent None.
_EXPORTS: dict[str, str] = {
    # spec
    "load_spec": "valkit.spec",
    "load_spec_from_string": "valkit.spec",
    "assess_risk": "valkit.spec",
    # statistics
    "wilson_lower": "valkit.stats",
    "wilson_interval": "valkit.stats",
    "clopper_pearson_lower": "valkit.stats",
    "clopper_pearson_interval": "valkit.stats",
    "min_n_zero_failures": "valkit.stats",
    "min_n_with_failures": "valkit.stats",
    "cohen_kappa": "valkit.stats",
    "evaluate_metric": "valkit.stats",
    "evaluate_acceptance": "valkit.stats",
    # evaluation
    "EvalRunner": "valkit.evals",
    "load_dataset": "valkit.evals",
    "FixtureProvider": "valkit.evals",
    # evidence and records
    "AuditTrail": "valkit.audit",
    "EvidenceVault": "valkit.vault",
    "SignatureService": "valkit.esign",
    # documents and traceability
    "DocumentGenerator": "valkit.docgen",
    "TraceabilityGraph": "valkit.trace",
    # monitoring
    "DriftMonitor": "valkit.drift",
    "ChangeControlRegister": "valkit.change",
    # orchestration
    "ValidationPipeline": "valkit.pipeline",
    "validate_agent": "valkit.pipeline",
}

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .audit import AuditTrail
    from .change import ChangeControlRegister
    from .docgen import DocumentGenerator
    from .drift import DriftMonitor
    from .esign import SignatureService
    from .evals import EvalRunner, FixtureProvider, load_dataset
    from .pipeline import ValidationPipeline, validate_agent
    from .spec import assess_risk, load_spec, load_spec_from_string
    from .stats import (
        clopper_pearson_interval,
        clopper_pearson_lower,
        cohen_kappa,
        evaluate_acceptance,
        evaluate_metric,
        min_n_with_failures,
        min_n_zero_failures,
        wilson_interval,
        wilson_lower,
    )
    from .trace import TraceabilityGraph
    from .vault import EvidenceVault


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module 'valkit' has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = ["__version__", *sorted(_EXPORTS)]
