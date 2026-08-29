"""The MCP server.

A thin adapter: the registry in :mod:`valkit.mcp.tools` is the source of truth,
so adding a tool requires no change here. The ``mcp`` package is imported
lazily, and its absence produces a clear ValKit error rather than an
ImportError at module import time.
"""

from __future__ import annotations

import sys
from typing import Any

from ..errors import ValKitError
from ..util import canonical_json
from .tools import ToolRegistry, ValKitToolContext, build_registry

__all__ = ["build_server", "main", "default_context"]


def default_context(workspace: str = ".valkit") -> ValKitToolContext:
    """A context backed by a local workspace.

    Assembled here rather than in the tools so that a caller embedding ValKit in
    their own server can supply collaborators of their own.
    """
    from pathlib import Path

    from ..audit.store import AuditTrail
    from ..change.control import ChangeControlRegister
    from ..docgen.generator import DocumentGenerator
    from ..drift.monitor import DriftMonitor, JsonMonitoringStore
    from ..esign.identity import StaticIdentityStore
    from ..esign.signatures import SignatureService
    from ..util import SystemClock
    from ..vault.store import EvidenceVault

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    audit = AuditTrail(root / "audit.sqlite", clock)

    return ValKitToolContext(
        clock=clock,
        vault=EvidenceVault(root / "vault", clock),
        audit=audit,
        signatures=SignatureService(StaticIdentityStore(clock), clock, audit),
        generator=DocumentGenerator(clock=clock),
        monitor=DriftMonitor(
            JsonMonitoringStore(root / "monitoring.jsonl"), clock=clock, audit=audit
        ),
        change_register=ChangeControlRegister(root / "changes.jsonl", clock=clock, audit=audit),
    )


def build_server(
    context: ValKitToolContext | None = None, registry: ToolRegistry | None = None
) -> Any:
    """Construct a FastMCP server exposing every tool in the registry."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as error:
        raise ValKitError(
            "the MCP server requires the 'mcp' package, which is not installed. "
            "Install it with: pip install 'valkit[mcp]'"
        ) from error

    context = context or default_context()
    registry = registry or build_registry()
    server = FastMCP("valkit")

    for definition in registry.definitions():
        _register(server, registry, definition, context)

    return server


def _register(server: Any, registry: ToolRegistry, definition: Any, context: Any) -> None:
    """Bind one registry tool onto the server.

    In its own function so that ``definition`` is captured per iteration rather
    than by reference to the loop variable.
    """

    def handler(**arguments: Any) -> str:
        result = registry.call(definition.name, arguments, context)
        return canonical_json(result)

    handler.__name__ = definition.name.replace(".", "_")
    handler.__doc__ = definition.description
    server.add_tool(handler, name=definition.name, description=definition.description)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry
    """Run the server over stdio."""
    import argparse

    parser = argparse.ArgumentParser(prog="valkit-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--workspace", default=".valkit")
    args = parser.parse_args(argv)

    try:
        server = build_server(default_context(args.workspace))
    except ValKitError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
