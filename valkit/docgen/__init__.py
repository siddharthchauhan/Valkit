"""Generating the signed validation package.

This is where evaluation results become the deliverable. Rendering is strict
(an undefined template variable raises rather than rendering an empty string)
and deterministic (the same records and clock produce byte-identical output, so
a regenerated document can be compared against the signed one).
"""

from __future__ import annotations

from .context import REQUIRED_FIELDS, DocumentContext, build_context
from .filters import FILTERS
from .generator import PACKAGE_ORDER, TEMPLATE_DIR, DocumentGenerator, markdown_to_html

__all__ = [
    "DocumentGenerator",
    "DocumentContext",
    "build_context",
    "REQUIRED_FIELDS",
    "PACKAGE_ORDER",
    "TEMPLATE_DIR",
    "markdown_to_html",
    "FILTERS",
]
