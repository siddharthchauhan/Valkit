"""The ValKit domain model.

Every module in ValKit reads and writes the types defined here, and nothing
else is shared between them. The shape of these records is deliberately close
to the shape of the regulatory artefacts they support:

* :class:`AgentSpec` mirrors the ``valkit.yaml`` a team commits next to their
  agent, and carries the context of use that FDA's January 2025 draft guidance
  makes step 2 of its credibility framework.
* :class:`Requirement`, :class:`Risk`, :class:`TestCase` and
  :class:`TestExecution` are the four columns of a GAMP 5 requirements-to-test
  traceability matrix.
* :class:`Signature` carries exactly the fields 21 CFR 11.50 requires a signed
  record to display, plus the record link 11.70 requires it to keep.
* :class:`AuditRecord` is one link in the append-only chain behind 11.10(e).

The dataclasses are plain stdlib. Parsing and validation live in
``valkit.spec``; these types only describe the data.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .util import canonical_json, sha256_obj, to_jsonable

__all__ = [
    # enums
    "GampCategory",
    "RiskLevel",
    "RiskClass",
    "RegulatoryImpact",
    "MetricType",
    "BoundMethod",
    "RunStatus",
    "RequirementKind",
    "QualificationPhase",
    "DocumentType",
    "DocumentStatus",
    "SignatureMeaning",
    "ValidationStatus",
    "SpcRule",
    "AlertSeverity",
    "ChangeControlStatus",
    "ChangeTrigger",
    # spec
    "SpecMetadata",
    "ContextOfUse",
    "IntendedUse",
    "GampSpec",
    "ModelsSpec",
    "DatasetSpec",
    "DatasetsSpec",
    "MetricSpec",
    "JudgeCalibrationSpec",
    "AcceptanceSpec",
    "MonitoringSpec",
    "SignoffSpec",
    "AgentSpec",
    # data + evaluation
    "GoldenSample",
    "Dataset",
    "Score",
    "SampleResult",
    "HarnessInfo",
    "StratumResult",
    "MetricResult",
    "JudgeCalibration",
    "EvalRun",
    # validation artefacts
    "Requirement",
    "Risk",
    "TestCase",
    "TestExecution",
    "Deviation",
    "Document",
    "Signature",
    "AuditRecord",
    "EvidenceRecord",
    "TraceLink",
    "DriftPoint",
    "SpcViolation",
    "DriftAlert",
    "ChangeControl",
    "PeriodicReview",
    "ValidationRecord",
    # helpers
    "Record",
]


# ==========================================================================
# Enumerations
# ==========================================================================


class GampCategory(int, Enum):
    """GAMP 5 (2nd edition) software categories.

    Category 2 was retired in GAMP 5 and is intentionally absent.
    """

    INFRASTRUCTURE = 1
    NON_CONFIGURED = 3
    CONFIGURED = 4
    BESPOKE = 5


class RiskLevel(str, Enum):
    """Ordinal level used for severity, probability and detectability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


class RiskClass(str, Enum):
    """Overall risk classification of an agent version or a single risk."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


class RegulatoryImpact(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"none": 0, "low": 1, "medium": 2, "high": 3}[self.value]


class MetricType(str, Enum):
    """How a metric is aggregated across samples."""

    PROPORTION = "proportion"
    """Pass/fail per sample; acceptance is a lower confidence bound."""

    MEAN = "mean"
    """Continuous score per sample; acceptance is a bound on the mean."""

    NUMERIC_TOLERANCE = "numeric_tolerance"
    """Per-sample numeric comparison within tolerance, aggregated as a proportion."""

    COUNT = "count"
    """Absolute count of occurrences (e.g. P1 defects), acceptance is a maximum."""


class BoundMethod(str, Enum):
    """Interval method used to turn observed results into an acceptance claim."""

    CLOPPER_PEARSON_LOWER = "clopper_pearson_lower"
    WILSON_LOWER = "wilson_lower"
    WALD_LOWER = "wald_lower"
    JEFFREYS_LOWER = "jeffreys_lower"
    STUDENT_T_LOWER = "student_t_lower"
    NON_INFERIORITY = "non_inferiority"
    NONE = "none"
    """Point estimate compared directly to target; not defensible for GxP use."""


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class RequirementKind(str, Enum):
    USER = "user"
    """URS — what the user needs the agent to do."""

    FUNCTIONAL = "functional"
    """FRS — how the system provides it."""

    REGULATORY = "regulatory"
    """A requirement imposed directly by a regulation or standard."""


class QualificationPhase(str, Enum):
    IQ = "IQ"
    OQ = "OQ"
    PQ = "PQ"


class DocumentType(str, Enum):
    URS = "URS"
    FRS = "FRS"
    RISK_ASSESSMENT = "RISK_ASSESSMENT"
    VALIDATION_PLAN = "VALIDATION_PLAN"
    CREDIBILITY_PLAN = "CREDIBILITY_PLAN"
    CREDIBILITY_REPORT = "CREDIBILITY_REPORT"
    IQ_PROTOCOL = "IQ_PROTOCOL"
    IQ_REPORT = "IQ_REPORT"
    OQ_PROTOCOL = "OQ_PROTOCOL"
    OQ_REPORT = "OQ_REPORT"
    PQ_PROTOCOL = "PQ_PROTOCOL"
    PQ_REPORT = "PQ_REPORT"
    RTM = "RTM"
    VSR = "VSR"
    PERIODIC_REVIEW = "PERIODIC_REVIEW"
    CHANGE_CONTROL = "CHANGE_CONTROL"
    TOOL_QUALIFICATION = "TOOL_QUALIFICATION"


class DocumentStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SignatureMeaning(str, Enum):
    """21 CFR 11.50(a)(3) — the meaning associated with the signature."""

    AUTHORED = "authored"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    EXECUTED = "executed"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ValidationStatus(str, Enum):
    DRAFT = "draft"
    IN_VALIDATION = "in_validation"
    VALIDATED = "validated"
    MONITORING_REVIEW = "monitoring_review"
    """Validated, but a drift rule has tripped and review is required."""

    INVALIDATED = "invalidated"
    RETIRED = "retired"


class SpcRule(str, Enum):
    """Statistical process control rule sets applied to monitoring series."""

    WESTERN_ELECTRIC = "western_electric"
    NELSON = "nelson"
    THRESHOLD = "threshold"
    """Fixed lower limit only — the acceptance target itself."""


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ChangeControlStatus(str, Enum):
    OPEN = "open"
    IMPACT_ASSESSED = "impact_assessed"
    EVAL_IN_PROGRESS = "eval_in_progress"
    EVAL_COMPLETE = "eval_complete"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLOSED = "closed"


class ChangeTrigger(str, Enum):
    MODEL_VERSION = "model_version"
    PROMPT_CHANGE = "prompt_change"
    DATASET_CHANGE = "dataset_change"
    SPEC_CHANGE = "spec_change"
    DRIFT = "drift"
    PERIODIC_REVIEW = "periodic_review"
    DEFECT = "defect"
    OTHER = "other"


# ==========================================================================
# Base helper
# ==========================================================================


@dataclass
class Record:
    """Mixin giving every domain record canonical serialisation and a digest."""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def to_json(self) -> str:
        return canonical_json(self)

    def digest(self) -> str:
        """SHA-256 over the canonical JSON form of this record."""
        return sha256_obj(self)

    def replace(self, **changes: Any):
        return dataclasses.replace(self, **changes)


# ==========================================================================
# Specification (valkit.yaml)
# ==========================================================================


@dataclass
class SpecMetadata(Record):
    agent_id: str
    version: str
    owner: str = ""
    system_of_record: str = ""
    description: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class ContextOfUse(Record):
    """FDA credibility-assessment steps 1 and 2.

    ``question_of_interest`` is the specific question the agent's output helps
    answer; ``role`` states how the output is used and by whom, including
    whether a human reviews it. ``model_influence`` and
    ``decision_consequence`` are the two axes of the model risk matrix.
    """

    question_of_interest: str
    role: str
    model_influence: RiskLevel = RiskLevel.MEDIUM
    decision_consequence: RiskLevel = RiskLevel.MEDIUM
    regulatory_impact: RegulatoryImpact = RegulatoryImpact.MEDIUM
    human_in_the_loop: bool = True
    patient_safety_impact: bool = False
    product_quality_impact: bool = False
    data_integrity_impact: bool = True


@dataclass
class IntendedUse(Record):
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class GampSpec(Record):
    category: GampCategory = GampCategory.BESPOKE
    risk_class: RiskClass | None = None
    """Optional override. When absent the risk engine derives it."""

    rationale: str = ""


@dataclass
class ModelsSpec(Record):
    primary: str = ""
    judge: str | None = None
    phi_safe_local: str | None = None
    temperature: float = 0.0
    seed: int | None = 0
    max_tokens: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetSpec(Record):
    ref: str
    sha256: str | None = None
    """Pinned digest. When set, loading fails if the file does not match."""

    version: str | None = None
    description: str = ""


@dataclass
class DatasetsSpec(Record):
    golden_set: DatasetSpec | None = None
    red_team: DatasetSpec | None = None
    calibration_set: DatasetSpec | None = None
    additional: dict[str, DatasetSpec] = field(default_factory=dict)


@dataclass
class MetricSpec(Record):
    """One acceptance criterion.

    A proportion metric asks: is the true pass rate at least ``target``, with
    ``confidence``? The answer is the one-sided lower confidence bound
    computed by ``method``, which is what gets written into the OQ.
    """

    name: str
    type: MetricType = MetricType.PROPORTION
    target: float | None = None
    confidence: float = 0.95
    method: BoundMethod = BoundMethod.CLOPPER_PEARSON_LOWER
    scorer: str | None = None
    """Name of the scorer producing this metric. Defaults to ``name``."""

    tolerance_abs: float | None = None
    tolerance_rel: float | None = None
    max_failures: int | None = None
    """Optional cap on absolute failures, independent of the bound."""

    max_count: int | None = None
    """For COUNT metrics: the highest acceptable observed count."""

    baseline: float | None = None
    margin: float | None = None
    """Non-inferiority margin delta, used with ``baseline``."""

    strata: list[str] = field(default_factory=list)
    """Sample metadata keys to break the metric down by."""

    critical: bool = True
    """A non-critical metric is reported but does not gate acceptance."""

    description: str = ""

    @property
    def scorer_name(self) -> str:
        return self.scorer or self.name


@dataclass
class JudgeCalibrationSpec(Record):
    """Gate on LLM-as-judge agreement with human labels."""

    min_cohen_kappa: float = 0.80
    min_percent_agreement: float | None = None
    min_samples: int = 30
    required: bool = True


@dataclass
class AcceptanceSpec(Record):
    metrics: list[MetricSpec] = field(default_factory=list)
    judge_calibration: JudgeCalibrationSpec | None = None

    def metric(self, name: str) -> MetricSpec | None:
        for spec in self.metrics:
            if spec.name == name:
                return spec
        return None


@dataclass
class MonitoringSpec(Record):
    schedule: str | None = None
    """Cron expression for scheduled re-evaluation."""

    spc_rule: SpcRule = SpcRule.WESTERN_ELECTRIC
    window: int = 20
    """Number of historical points used to establish control limits."""

    alert_channels: list[str] = field(default_factory=list)
    auto_change_control: bool = True
    periodic_review_months: int = 6


@dataclass
class SignoffSpec(Record):
    approvers: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    esignature: str = "part11"
    require_distinct_signers: bool = True
    """Author and approver must not be the same person (segregation of duties)."""


@dataclass
class AgentSpec(Record):
    """The complete parsed ``valkit.yaml``."""

    metadata: SpecMetadata
    context_of_use: ContextOfUse
    intended_use: IntendedUse = field(default_factory=IntendedUse)
    gamp: GampSpec = field(default_factory=GampSpec)
    models: ModelsSpec = field(default_factory=ModelsSpec)
    datasets: DatasetsSpec = field(default_factory=DatasetsSpec)
    acceptance: AcceptanceSpec = field(default_factory=AcceptanceSpec)
    monitoring: MonitoringSpec = field(default_factory=MonitoringSpec)
    signoff: SignoffSpec = field(default_factory=SignoffSpec)
    api_version: str = "valkit/v1"
    kind: str = "AgentValidation"
    source_sha256: str | None = None
    """Digest of the raw YAML the spec was parsed from."""

    @property
    def agent_id(self) -> str:
        return self.metadata.agent_id

    @property
    def version(self) -> str:
        return self.metadata.version

    @property
    def ref(self) -> str:
        """Stable ``agent_id@version`` reference."""
        return f"{self.metadata.agent_id}@{self.metadata.version}"


# ==========================================================================
# Datasets and evaluation
# ==========================================================================


@dataclass
class GoldenSample(Record):
    """One labelled case in a golden or red-team set."""

    sample_id: str
    input: Any
    target: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    contains_phi: bool = False
    stratum: str | None = None
    human_label: float | None = None
    """Human pass/fail (1.0/0.0) or score, used to calibrate the judge."""

    tags: list[str] = field(default_factory=list)


@dataclass
class Dataset(Record):
    name: str
    ref: str
    sha256: str
    samples: list[GoldenSample] = field(default_factory=list)
    version: str | None = None
    description: str = ""

    def __len__(self) -> int:
        return len(self.samples)

    @property
    def phi_count(self) -> int:
        return sum(1 for s in self.samples if s.contains_phi)

    @property
    def labelled_count(self) -> int:
        return sum(1 for s in self.samples if s.human_label is not None)


@dataclass
class Score(Record):
    """The result of applying one scorer to one sample."""

    value: float
    passed: bool
    explanation: str = ""
    scorer: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SampleResult(Record):
    sample_id: str
    output: str = ""
    scores: dict[str, Score] = field(default_factory=dict)
    stratum: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    transcript_ref: str | None = None

    def score_for(self, scorer: str) -> Score | None:
        return self.scores.get(scorer)


@dataclass
class HarnessInfo(Record):
    """Identifies the evaluation harness, for installation qualification.

    An OQ result is only meaningful if you can say which harness produced it
    and prove that harness was the qualified one.
    """

    name: str = "valkit"
    version: str = "0.1.0"
    provider: str = ""
    config_sha256: str = ""
    python_version: str = ""
    platform: str = ""


@dataclass
class StratumResult(Record):
    key: str
    value: str
    n: int
    k: int
    point_estimate: float
    lower_bound: float | None = None
    passed: bool | None = None


@dataclass
class MetricResult(Record):
    """An acceptance decision for one metric on one run."""

    name: str
    type: MetricType
    n: int
    k: int
    point_estimate: float
    method: BoundMethod
    confidence: float
    passed: bool
    target: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    failures: int = 0
    errors: int = 0
    critical: bool = True
    rationale: str = ""
    strata: list[StratumResult] = field(default_factory=list)
    failing_sample_ids: list[str] = field(default_factory=list)


@dataclass
class JudgeCalibration(Record):
    """Agreement between the LLM judge and human labels."""

    judge_model: str
    n: int
    cohen_kappa: float
    percent_agreement: float
    min_required: float
    passed: bool
    confusion: dict[str, int] = field(default_factory=dict)
    """Keys ``tp``, ``fp``, ``tn``, ``fn`` for binary labels."""

    note: str = ""


@dataclass
class EvalRun(Record):
    """One complete execution of the acceptance battery."""

    run_id: str
    agent_id: str
    agent_version: str
    dataset_ref: str
    dataset_sha256: str
    model: str
    status: RunStatus = RunStatus.PENDING
    started_at: str = ""
    finished_at: str | None = None
    spec_sha256: str | None = None
    judge_model: str | None = None
    seed: int | None = None
    samples: list[SampleResult] = field(default_factory=list)
    metrics: list[MetricResult] = field(default_factory=list)
    calibration: JudgeCalibration | None = None
    harness: HarnessInfo = field(default_factory=HarnessInfo)
    environment: dict[str, Any] = field(default_factory=dict)
    transcripts_ref: str | None = None
    error: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when every critical metric passed and calibration held."""
        if self.status is not RunStatus.COMPLETED:
            return False
        if self.calibration is not None and not self.calibration.passed:
            return False
        critical = [m for m in self.metrics if m.critical]
        if not critical:
            return False
        return all(m.passed for m in critical)

    def metric(self, name: str) -> MetricResult | None:
        for result in self.metrics:
            if result.name == name:
                return result
        return None


# ==========================================================================
# Validation artefacts
# ==========================================================================


@dataclass
class Requirement(Record):
    req_id: str
    kind: RequirementKind
    text: str
    rationale: str = ""
    source: str = ""
    """Where the requirement came from — spec path, regulation, or SME."""

    gxp_impact: bool = True
    critical: bool = True
    parent_ids: list[str] = field(default_factory=list)
    """FRS entries point at the URS entries they implement."""


@dataclass
class Risk(Record):
    risk_id: str
    description: str
    failure_mode: str
    severity: RiskLevel = RiskLevel.MEDIUM
    probability: RiskLevel = RiskLevel.MEDIUM
    detectability: RiskLevel = RiskLevel.MEDIUM
    risk_class: RiskClass = RiskClass.MEDIUM
    mitigation: str = ""
    residual_risk: RiskClass = RiskClass.LOW
    requirement_ids: list[str] = field(default_factory=list)
    category: str = ""
    """e.g. hallucination, prompt-injection, drift, data-integrity."""


@dataclass
class TestCase(Record):
    # Tells pytest not to try to collect this as a test class. The name comes
    # from the validation domain, where a test case is an IQ/OQ/PQ step.
    __test__ = False

    test_id: str
    phase: QualificationPhase
    title: str
    objective: str
    acceptance_text: str
    requirement_ids: list[str] = field(default_factory=list)
    risk_ids: list[str] = field(default_factory=list)
    metric_name: str | None = None
    prerequisites: list[str] = field(default_factory=list)
    procedure: list[str] = field(default_factory=list)
    expected_result: str = ""
    scripted: bool = True
    """False for unscripted/exploratory testing per FDA CSA."""


@dataclass
class Deviation(Record):
    deviation_id: str
    test_id: str
    description: str
    severity: RiskLevel = RiskLevel.MEDIUM
    sample_ids: list[str] = field(default_factory=list)
    disposition: str = ""
    capa_ref: str | None = None
    closed: bool = False


@dataclass
class TestExecution(Record):
    __test__ = False

    test_id: str
    run_id: str
    executed_at: str
    passed: bool
    observed_result: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    deviations: list[Deviation] = field(default_factory=list)
    executed_by: str = ""
    harness: HarnessInfo | None = None


@dataclass
class EvidenceRecord(Record):
    """One immutable object in the evidence vault."""

    evidence_id: str
    kind: str
    """e.g. transcript, dataset, metrics, document, spec, manifest."""

    sha256: str
    size_bytes: int
    stored_at: str
    uri: str
    content_type: str = "application/json"
    retention_until: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signature(Record):
    """A 21 CFR Part 11 electronic signature.

    ``printed_name``, ``signed_at`` and ``meaning`` are the three items
    11.50(a) requires the signed record to display. ``document_sha256`` is the
    11.70 link: the signature is bound to the exact bytes that were signed, so
    it cannot be excised, copied or transferred to another record.
    """

    signature_id: str
    document_id: str
    document_sha256: str
    signer_id: str
    printed_name: str
    meaning: SignatureMeaning
    signed_at: str
    components_used: list[str] = field(default_factory=list)
    """Which identification components were supplied — never their values."""

    session_id: str = ""
    is_first_in_session: bool = True
    manifest_sha256: str = ""
    """Digest over the full signature manifest, written to the audit trail."""

    reason: str = ""
    role: str = ""


@dataclass
class AuditRecord(Record):
    """One link in the append-only, hash-chained audit trail."""

    seq: int
    ts: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    row_hash: str = ""
    reason: str | None = None


@dataclass
class Document(Record):
    doc_id: str
    doc_type: DocumentType
    title: str
    agent_id: str
    agent_version: str
    content: str
    content_sha256: str
    generated_at: str
    version: str = "1.0"
    status: DocumentStatus = DocumentStatus.DRAFT
    template: str = ""
    format: str = "markdown"
    evidence_refs: list[str] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)
    run_id: str | None = None
    supersedes: str | None = None

    @property
    def approved(self) -> bool:
        return self.status is DocumentStatus.APPROVED


@dataclass
class TraceLink(Record):
    """A directed edge in the traceability graph."""

    source_type: str
    source_id: str
    relation: str
    target_type: str
    target_id: str


@dataclass
class DriftPoint(Record):
    """One observation in a monitored metric series."""

    agent_id: str
    metric: str
    observed_at: str
    value: float
    run_id: str | None = None
    n: int | None = None
    lower_bound: float | None = None


@dataclass
class SpcViolation(Record):
    rule: str
    rule_set: SpcRule
    index: int
    observed_at: str
    value: float
    description: str
    severity: AlertSeverity = AlertSeverity.WARNING


@dataclass
class DriftAlert(Record):
    alert_id: str
    agent_id: str
    metric: str
    raised_at: str
    severity: AlertSeverity
    violations: list[SpcViolation] = field(default_factory=list)
    center_line: float | None = None
    lower_control_limit: float | None = None
    upper_control_limit: float | None = None
    change_control_id: str | None = None
    channels: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ChangeControl(Record):
    cc_id: str
    agent_id: str
    agent_version: str
    trigger: ChangeTrigger
    reason: str
    opened_at: str
    status: ChangeControlStatus = ChangeControlStatus.OPEN
    impact: str = ""
    required_scope: list[str] = field(default_factory=list)
    """Metric names that must be re-run before validated status is restored."""

    run_ids: list[str] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)
    closed_at: str | None = None
    outcome: str = ""
    prior_version: str | None = None
    new_version: str | None = None


@dataclass
class PeriodicReview(Record):
    review_id: str
    agent_id: str
    period_start: str
    period_end: str
    generated_at: str
    scheduled_runs: int = 0
    executed_runs: int = 0
    drift_events: int = 0
    change_controls: int = 0
    deviations: int = 0
    kappa_min: float | None = None
    kappa_max: float | None = None
    conclusion: str = ""
    remains_validated: bool = True
    next_review_due: str | None = None


@dataclass
class ValidationRecord(Record):
    """The complete validated state of one agent version.

    This is what the pipeline produces and what the vault, the RTM and the
    validation summary report are all rendered from.
    """

    agent_id: str
    agent_version: str
    spec: AgentSpec
    status: ValidationStatus = ValidationStatus.DRAFT
    risk_class: RiskClass = RiskClass.MEDIUM
    gamp_category: GampCategory = GampCategory.BESPOKE
    requirements: list[Requirement] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)
    executions: list[TestExecution] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    links: list[TraceLink] = field(default_factory=list)
    change_controls: list[ChangeControl] = field(default_factory=list)
    validated_at: str | None = None
    created_at: str = ""

    def document(self, doc_type: DocumentType) -> Document | None:
        for doc in self.documents:
            if doc.doc_type is doc_type:
                return doc
        return None

    def requirement(self, req_id: str) -> Requirement | None:
        for req in self.requirements:
            if req.req_id == req_id:
                return req
        return None
