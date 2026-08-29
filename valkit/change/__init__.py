"""Change control.

Any change to the model, prompt, qualification data or specification puts a
validated status in question. This package derives the required re-evaluation
scope from the kind of change, and refuses to approve a change whose scope has
not actually been covered by a passing run.
"""

from __future__ import annotations

from .control import (
    ALLOWED_TRANSITIONS,
    REQUIRED_SCOPE,
    ChangeControlRegister,
    ScopeAssessment,
    changed_metric_names,
    version_diff,
)

__all__ = [
    "ChangeControlRegister",
    "ScopeAssessment",
    "version_diff",
    "changed_metric_names",
    "REQUIRED_SCOPE",
    "ALLOWED_TRANSITIONS",
]
