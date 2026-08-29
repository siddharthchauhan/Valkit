"""Request and response bodies.

Two conventions run through this module.

**Credentials appear in exactly one place.** ``SignRequest.components`` is the
only field in the API that carries a signature component, it is only ever read
from a request body, and no response model has a field that could hold one. The
validation-error handler in :mod:`api.main` redacts the field before FastAPI
echoes a malformed body back, because the default behaviour of returning the
offending input is a leak when the input is a password.

**Responses state the reasoning, not just the verdict.** A response that says
``ready: false`` without the blockers is useless to the person who has to act on
it, so the blockers, the satisfied conditions and the outstanding obligations
are all carried.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from valkit.models import ChangeTrigger, SignatureMeaning

__all__ = [
    "IngestSpecRequest",
    "SpecSummary",
    "StartValidationRequest",
    "ValidationSummary",
    "ReadinessModel",
    "MetricModel",
    "RunSummary",
    "DocumentSummary",
    "SignRequest",
    "SignatureResponse",
    "RegisterSignerRequest",
    "DriftPointModel",
    "DriftResponse",
    "ChangeControlRequest",
    "ChangeControlResponse",
    "VerificationResponse",
    "ErrorResponse",
    "HealthResponse",
]


class Strict(BaseModel):
    """Reject unknown fields.

    A silently ignored field in a request that produces a signed document is a
    defect nobody sees until an audit.
    """

    model_config = ConfigDict(extra="forbid")


# -- specifications --------------------------------------------------------


class IngestSpecRequest(Strict):
    yaml: str = Field(description="The valkit.yaml content.")
    strict: bool = Field(default=True, description="Reject unknown keys in the specification.")


class SpecSummary(BaseModel):
    ref: str
    agent_id: str
    version: str
    gamp_category: int
    risk_class: str
    derived_risk_class: str
    requirements: int
    risks: int
    tests: int
    spec_sha256: str
    warnings: list[str] = []


# -- validations -----------------------------------------------------------


class StartValidationRequest(Strict):
    spec_ref: str = Field(description="The agent_id or agent_id@version to validate.")
    run_id: str | None = Field(default=None, description="Override the generated run identifier.")


class ReadinessModel(BaseModel):
    """Why a package is or is not validated.

    ``conditions`` are obligations that do not block the qualification evidence
    but which validated status depends on — chiefly the unscripted performance
    qualification steps that can only be performed once the system is in
    operation. They are reported rather than hidden.
    """

    ready: bool
    blockers: list[str]
    satisfied: list[str]
    conditions: list[str]


class MetricModel(BaseModel):
    name: str
    k: int
    n: int
    point_estimate: float
    lower_bound: float
    target: float
    confidence: float
    method: str
    passed: bool
    critical: bool
    errors: int
    rationale: str


class CalibrationModel(BaseModel):
    cohen_kappa: float
    min_required: float
    passed: bool
    agreement: float
    n: int


class RunSummary(BaseModel):
    run_id: str
    status: str
    passed: bool
    started_at: str
    model: str
    dataset_sha256: str
    transcripts_ref: str | None = None
    metrics: list[MetricModel] = []
    calibration: CalibrationModel | None = None


class DocumentSummary(BaseModel):
    doc_id: str
    doc_type: str
    title: str
    version: str
    status: str
    content_sha256: str
    generated_at: str
    signature_count: int
    signatures_required_met: bool


class ValidationSummary(BaseModel):
    validation_id: str
    agent_id: str
    agent_version: str
    status: str
    created_at: str
    validated_at: str | None = None
    readiness: ReadinessModel
    run: RunSummary | None = None
    documents: list[DocumentSummary] = []
    skipped_documents: dict[str, str] = {}
    warnings: list[str] = []


# -- signatures ------------------------------------------------------------


class SignRequest(Strict):
    """A 21 CFR Part 11 subpart C signing.

    ``components`` is the only field in this API that carries a credential. It
    is read from the request body and passed straight to the signature service;
    nothing derived from it is returned, logged or written to the audit trail.
    """

    user: str = Field(description="The signer's identification code.")
    meaning: SignatureMeaning = Field(
        description="The meaning associated with the signature (21 CFR 11.50(a)(3))."
    )
    components: dict[str, str] = Field(
        description=(
            "Signature components. The first signing of a session needs all of them; "
            "a subsequent signing within the same session needs at least the one only "
            "the individual can execute."
        )
    )
    reason: str = Field(default="", description="Optional free-text reason, recorded.")
    session_id: str | None = Field(
        default=None, description="An open signing session, for subsequent signings."
    )


class SignatureResponse(BaseModel):
    signature_id: str
    document_id: str
    document_sha256: str
    printed_name: str
    signed_at: str
    meaning: str
    components_used: list[str]
    manifest: str


class RegisterSignerRequest(Strict):
    """Register a signer.

    Present so the API is usable end to end, and deliberately narrow. A
    production deployment binds the identity store to the customer's directory;
    the identity-proofing and the 11.100(b)/(c) certification that must precede
    it are organisational controls no API call can supply.
    """

    user_id: str
    printed_name: str
    password: str
    roles: list[str] = []
    email: str = ""
    title: str = ""


class SignerSummary(BaseModel):
    user_id: str
    printed_name: str
    roles: list[str]
    active: bool
    components: list[str]


# -- monitoring and change control -----------------------------------------


class DriftPointModel(BaseModel):
    metric: str
    observed_at: str
    value: float
    n: int
    run_id: str | None = None


class ViolationModel(BaseModel):
    metric: str
    alert_id: str
    rule: str
    severity: str
    value: float
    description: str


class DriftResponse(BaseModel):
    agent_id: str
    points: list[DriftPointModel]
    violations: list[ViolationModel]


class ChangeControlRequest(Strict):
    agent_id: str
    reason: str
    # Typed as the enum so an unknown trigger is a 422 that names the valid
    # values, rather than a bare ValueError escaping the route as a 500.
    trigger: ChangeTrigger = ChangeTrigger.OTHER
    version: str = ""
    metrics: list[str] | None = None


class ChangeControlResponse(BaseModel):
    cc_id: str
    agent_id: str
    status: str
    trigger: str
    reason: str
    opened_at: str
    required_scope: list[str]
    impact: list[str]


# -- integrity and health --------------------------------------------------


class VerificationResponse(BaseModel):
    """The result of verifying stored evidence.

    Reported separately from acceptance throughout: "the evidence cannot be
    trusted" and "the agent did not meet its target" are different
    conversations.
    """

    ok: bool
    reason: str = ""
    checked: int = 0
    detail: dict[str, Any] = {}


class HealthResponse(BaseModel):
    status: str
    version: str
    detail: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: str
    error_type: str
    path: str | None = None
