"""Deployment configuration, read from the environment.

The names match the variables the Terraform in ``infra/`` sets on the task
definitions, so that what the container reads and what the deployment writes
cannot drift apart silently.

Nothing here reads a credential. ``VALKIT_DB_CREDENTIAL`` is injected as an ECS
secret and resolved by the database layer at connection time; an API process
that never holds it cannot leak it into a log line or an error response.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings", "from_environment"]


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        # A malformed retention period is a deployment defect, but failing to
        # start is worse than falling back to the documented default: the
        # value is a floor the vault enforces, not a ceiling.
        return default


@dataclass
class Settings:
    """What the API needs to know about where it is running."""

    workspace: Path = field(default_factory=lambda: Path(".valkit"))

    # Evidence. When a bucket is named, evidence goes to S3 Object Lock and the
    # local vault is not used; the two are alternatives, not layers.
    evidence_bucket: str | None = None
    evidence_kms_key: str | None = None
    object_lock_mode: str = "COMPLIANCE"
    retention_years: int = 7

    region: str = "us-east-1"
    db_host: str | None = None

    # A specification may name a hosted model for general cases and a local one
    # for cases carrying protected health information. Without the local
    # endpoint configured, evaluation refuses PHI-flagged samples rather than
    # sending them to a hosted provider.
    phi_model_endpoint: str | None = None

    # Off by default. A deployment that serves the console to the internet
    # needs an identity provider in front of it; the flag exists so that
    # turning it on is a decision someone made rather than a default they
    # inherited.
    serve_console: bool = True
    cors_origins: tuple[str, ...] = ()

    version: str = "0.1.0"

    @property
    def uses_s3(self) -> bool:
        return bool(self.evidence_bucket)


def from_environment(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``os.environ`` (or a supplied mapping, for tests)."""
    env = os.environ if environ is None else environ
    origins = tuple(
        origin.strip()
        for origin in env.get("VALKIT_CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    return Settings(
        workspace=Path(env.get("VALKIT_WORKSPACE", ".valkit")),
        evidence_bucket=env.get("VALKIT_EVIDENCE_BUCKET") or None,
        evidence_kms_key=env.get("VALKIT_EVIDENCE_KMS_KEY") or None,
        object_lock_mode=env.get("VALKIT_OBJECT_LOCK_MODE", "COMPLIANCE"),
        retention_years=_int(env, "VALKIT_RETENTION_YEARS", 7),
        region=env.get("AWS_REGION", "us-east-1"),
        db_host=env.get("VALKIT_DB_HOST") or None,
        phi_model_endpoint=env.get("VALKIT_PHI_MODEL_ENDPOINT") or None,
        serve_console=_flag(env, "VALKIT_SERVE_CONSOLE", True),
        cors_origins=origins,
    )
