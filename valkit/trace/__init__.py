"""Traceability: the graph, its validation, and the requirements-to-test matrix.

The graph's most useful function is finding where the chain breaks, since an
uncovered requirement is the commonest audit finding against a matrix and the
one a generated document could most easily hide.
"""

from __future__ import annotations

from .graph import (
    NODE_TYPES,
    Coverage,
    TraceabilityGraph,
    TraceFinding,
    TraceNode,
    TraceValidation,
)
from .rtm import RtmRow, build_rtm, natural_key, render_compact, render_csv, render_markdown

__all__ = [
    "TraceabilityGraph",
    "TraceNode",
    "TraceFinding",
    "TraceValidation",
    "Coverage",
    "NODE_TYPES",
    "RtmRow",
    "build_rtm",
    "render_markdown",
    "render_csv",
    "render_compact",
    "natural_key",
]
