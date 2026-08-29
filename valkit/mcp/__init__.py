"""The Model Context Protocol surface.

ValKit is MCP-native so that an engineer's own coding agent can drive a
validation from inside their editor, making the evidence a by-product of
building the agent rather than a separate project afterwards.

:mod:`tools` holds the transport-independent registry and is testable without
the ``mcp`` package; :mod:`server` is a thin stdio adapter over it.
"""

from __future__ import annotations

from .tools import (
    SchemaError,
    ToolDefinition,
    ToolRegistry,
    ValKitToolContext,
    build_registry,
    validate_against_schema,
)

__all__ = [
    "build_registry",
    "ToolRegistry",
    "ToolDefinition",
    "ValKitToolContext",
    "validate_against_schema",
    "SchemaError",
    "build_server",
    "default_context",
]


def __getattr__(name: str):
    """Expose the server lazily, so importing this package never needs ``mcp``."""
    if name in ("build_server", "default_context"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module 'valkit.mcp' has no attribute {name!r}")
