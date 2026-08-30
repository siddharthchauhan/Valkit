"""The service container and the FastAPI dependencies that reach into it.

One decision shapes this module. **The durable records are the audit trail and
the evidence vault; everything else the API holds is working state.** Pipelines,
derived requirements and rendered documents live in memory and are rebuilt from
the specification, which is itself in the vault. Nothing that an inspector would
ask to see depends on a process staying up: the hash-chained trail is on disk or
in Postgres, and the evidence is content-addressed in the vault or in S3 Object
Lock.

That is why this module is small. A service that persisted its working state as
well would have two sources of truth for the same package, and the interesting
question during an inspection would become which one to believe.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from valkit.audit.store import AuditTrail
from valkit.change.control import ChangeControlRegister
from valkit.drift.monitor import DriftMonitor, JsonMonitoringStore
from valkit.errors import ValKitError
from valkit.esign.identity import StaticIdentityStore
from valkit.esign.signatures import SignatureService
from valkit.evals.providers import judge_for_spec, provider_for_spec
from valkit.models import AgentSpec, Document
from valkit.pipeline import ValidationPipeline
from valkit.util import Clock, SystemClock
from valkit.vault.store import EvidenceVault

from .settings import Settings, from_environment

__all__ = ["Services", "build_services", "get_services", "set_services", "Validation"]


@dataclass
class Validation:
    """One validation in flight, and the pipeline that owns it."""

    validation_id: str
    agent_id: str
    agent_version: str
    pipeline: ValidationPipeline
    created_at: str
    warnings: list[str] = field(default_factory=list)

    @property
    def documents(self) -> list[Document]:
        record = self.pipeline.record
        return list(record.documents) if record else []

    def document(self, doc_id: str) -> Document | None:
        for document in self.documents:
            if document.doc_id == doc_id:
                return document
        return None


class Services:
    """Everything the routes need, assembled once at startup."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        audit: AuditTrail | None = None,
        vault: Any = None,
        identities: StaticIdentityStore | None = None,
        signatures: SignatureService | None = None,
        monitor: DriftMonitor | None = None,
        change_register: ChangeControlRegister | None = None,
        base_dir: Any = None,
    ):
        self.settings = settings
        self.clock = clock or SystemClock()
        workspace = settings.workspace
        workspace.mkdir(parents=True, exist_ok=True)

        self.audit = audit or AuditTrail(workspace / "audit.sqlite", self.clock)
        self.vault = vault if vault is not None else self._build_vault(workspace)
        self.identities = identities or StaticIdentityStore(self.clock)
        self.signatures = signatures or SignatureService(self.identities, self.clock, self.audit)
        self.monitor = monitor or DriftMonitor(
            JsonMonitoringStore(workspace / "monitoring.jsonl"), clock=self.clock
        )
        self.change_register = change_register or ChangeControlRegister(
            workspace / "change-control.json", clock=self.clock, audit=self.audit
        )
        self.base_dir = base_dir

        self.specs: dict[str, AgentSpec] = {}
        # Warnings are produced by the loader, not derivable from the parsed
        # specification, so they have to be kept when it is ingested or they are
        # lost on the next read. They are the statistical-validity notes — the
        # ones most worth not losing.
        self.spec_warnings: dict[str, list[str]] = {}
        self.validations: dict[str, Validation] = {}
        # Serialises the stages, which mutate a pipeline in place. Evaluation is
        # IO-bound on model calls, so this is not where throughput comes from;
        # the worker process is.
        self.lock = threading.RLock()
        self._counter = 0

    def _build_vault(self, workspace: Any) -> Any:
        settings = self.settings
        if not settings.uses_s3:
            return EvidenceVault(workspace / "vault", self.clock)

        from valkit.vault.s3 import S3EvidenceVault

        return S3EvidenceVault(
            bucket=settings.evidence_bucket,
            clock=self.clock,
            kms_key_id=settings.evidence_kms_key,
            object_lock_mode=settings.object_lock_mode,
            retention_years=settings.retention_years,
        )

    # -- registry ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}"

    def require_spec(self, ref: str) -> AgentSpec:
        for candidate in (ref, *(k for k in self.specs if k.split("@")[0] == ref)):
            if candidate in self.specs:
                return self.specs[candidate]
        raise ValKitError(f"no specification {ref!r} has been ingested")

    def require_validation(self, validation_id: str) -> Validation:
        validation = self.validations.get(validation_id)
        if validation is None:
            raise ValKitError(f"no validation with identifier {validation_id!r}")
        return validation

    def find_document(self, doc_id: str) -> tuple[Validation, Document]:
        for validation in self.validations.values():
            document = validation.document(doc_id)
            if document is not None:
                return validation, document
        raise ValKitError(f"no document with identifier {doc_id!r}")

    def new_pipeline(self, spec: AgentSpec) -> ValidationPipeline:
        return ValidationPipeline(
            provider=provider_for_spec(spec, base_dir=self.base_dir),
            judge=judge_for_spec(spec),
            vault=self.vault,
            audit=self.audit,
            signatures=self.signatures,
            change_register=self.change_register,
            clock=self.clock,
            base_dir=self.base_dir,
        )

    def close(self) -> None:
        try:
            self.audit.close()
        except Exception:  # pragma: no cover - shutdown must not raise
            pass


def build_services(settings: Settings | None = None, **kwargs: Any) -> Services:
    return Services(settings or from_environment(), **kwargs)


# -- FastAPI wiring --------------------------------------------------------
#
# Held on the app rather than in a module global so that two apps in one
# process (which is how the tests run) do not share a vault.

_SERVICES_KEY = "valkit_services"


def set_services(app: Any, services: Services) -> None:
    setattr(app.state, _SERVICES_KEY, services)


def get_services(request: Any) -> Services:
    services = getattr(request.app.state, _SERVICES_KEY, None)
    if services is None:  # pragma: no cover - only if create_app is bypassed
        raise RuntimeError("the application was started without a service container")
    return services
