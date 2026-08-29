"""The ValKit HTTP API and web console.

Kept out of the ``valkit`` package on purpose. The core library depends on
PyYAML and Jinja2 and nothing else, because every third-party package in a GxP
tool is a supplier that has to be assessed; FastAPI, Starlette, Pydantic and
uvicorn are a deployment concern rather than a library one. A customer who runs
ValKit from CI or from their own coding agent never installs them.

``pip install 'valkit[api]'`` adds them, and this package is the only thing that
imports them.
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str):
    # Lazy, so that ``import api`` does not require FastAPI to be installed —
    # the packaging test imports this module with the core dependencies only.
    if name == "create_app":
        from .main import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
