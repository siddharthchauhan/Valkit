"""Building the render context for a validation document.

Every number a generated document states comes from a record. Nothing is
recomputed in a template, and nothing is filled in with a plausible default:
where the context cannot supply a field a document requires, generation fails
with a :class:`~valkit.errors.DocumentError` naming the document type and the
field.

That strictness is the point. A validation document with a blank where an
acceptance bound should be is worse than no document, because it will be signed
anyway — a reviewer reading forty pages does not notice the one table cell that
did not render. Failing loudly at generation time is the only place the problem
is cheap to fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..errors import DocumentError
from ..models import (
    AgentSpec,
    ChangeControl,
    Deviation,
    Document,
    DocumentType,
    DriftAlert,
    EvalRun,
    EvidenceRecord,
    PeriodicReview,
    Requirement,
    RequirementKind,
    Risk,
    TestCase,
    TestExecution,
)
from ..util import Clock, SystemClock

__all__ = ["DocumentContext", "build_context", "REQUIRED_FIELDS"]


# What each document type cannot be rendered without. Checked before rendering
# so the failure names the missing input rather than surfacing as an undefined
# variable inside a template.
REQUIRED_FIELDS: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.URS: ("requirements",),
    DocumentType.FRS: ("requirements",),
    DocumentType.RISK_ASSESSMENT: ("assessment", "risks"),
    DocumentType.VALIDATION_PLAN: ("assessment", "requirements", "tests"),
    DocumentType.CREDIBILITY_PLAN: ("assessment", "metrics"),
    DocumentType.CREDIBILITY_REPORT: ("assessment", "run"),
    DocumentType.IQ_PROTOCOL: ("tests",),
    DocumentType.IQ_REPORT: ("tests", "executions"),
    DocumentType.OQ_PROTOCOL: ("tests", "metrics"),
    DocumentType.OQ_REPORT: ("tests", "executions", "run"),
    DocumentType.PQ_PROTOCOL: ("tests",),
    DocumentType.PQ_REPORT: ("tests", "executions"),
    DocumentType.RTM: ("rtm_rows", "coverage"),
    DocumentType.VSR: ("run", "assessment", "coverage"),
    DocumentType.PERIODIC_REVIEW: ("periodic_review",),
    DocumentType.CHANGE_CONTROL: ("change_control",),
    DocumentType.TOOL_QUALIFICATION: (),
}


@dataclass
class DocumentContext:
    """Everything a validation document may draw on."""

    spec: AgentSpec
    generated_at: str
    doc_id: str = ""
    doc_type: DocumentType | None = None
    version: str = "1.0"

    assessment: Any = None
    requirements: list[Requirement] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)
    executions: list[TestExecution] = field(default_factory=list)
    run: EvalRun | None = None
    runs: list[EvalRun] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    deviations: list[Deviation] = field(default_factory=list)
    change_controls: list[ChangeControl] = field(default_factory=list)
    change_control: ChangeControl | None = None
    drift_alerts: list[DriftAlert] = field(default_factory=list)
    periodic_review: PeriodicReview | None = None
    dataset_summary: Any = None
    redteam_summary: Any = None
    rtm_rows: list[Any] = field(default_factory=list)
    coverage: Any = None
    trace_validation: Any = None
    documents: list[Document] = field(default_factory=list)
    signature_block: str = ""
    tool_version: str = "0.1.0"
    document_title: str = ""

    # -- derived views the templates use ----------------------------------

    @property
    def user_requirements(self) -> list[Requirement]:
        return [r for r in self.requirements if r.kind is RequirementKind.USER]

    @property
    def functional_requirements(self) -> list[Requirement]:
        return [r for r in self.requirements if r.kind is RequirementKind.FUNCTIONAL]

    @property
    def regulatory_requirements(self) -> list[Requirement]:
        return [r for r in self.requirements if r.kind is RequirementKind.REGULATORY]

    def tests_for(self, phase: str) -> list[TestCase]:
        return [t for t in self.tests if t.phase.value == phase]

    def executions_for(self, phase: str) -> list[TestExecution]:
        ids = {t.test_id for t in self.tests_for(phase)}
        return [e for e in self.executions if e.test_id in ids]

    def execution_for(self, test_id: str) -> TestExecution | None:
        for execution in self.executions:
            if execution.test_id == test_id:
                return execution
        return None

    def test_for(self, test_id: str) -> TestCase | None:
        for test in self.tests:
            if test.test_id == test_id:
                return test
        return None

    @property
    def metrics(self) -> list[Any]:
        return list(self.spec.acceptance.metrics)

    @property
    def metric_results(self) -> list[Any]:
        return list(self.run.metrics) if self.run else []

    @property
    def all_deviations(self) -> list[Deviation]:
        collected = list(self.deviations)
        for execution in self.executions:
            collected.extend(execution.deviations)
        return collected

    @property
    def rtm_markdown(self) -> str:
        """The matrix, rendered by the traceability module rather than a template.

        The RTM's layout is shared with the CLI and the API, so it is rendered
        in one place. A template that rebuilt the table would be a second
        implementation of the coverage rules, free to disagree with the first.
        """
        from ..trace.rtm import render_markdown

        return render_markdown(self.rtm_rows, self.coverage)

    @property
    def critical_metrics_passed(self) -> bool:
        if not self.run:
            return False
        critical = [m for m in self.run.metrics if m.critical]
        return bool(critical) and all(m.passed for m in critical)

    def to_dict(self) -> dict[str, Any]:
        """The mapping handed to Jinja."""
        return {
            "spec": self.spec,
            "metadata": self.spec.metadata,
            "context_of_use": self.spec.context_of_use,
            "intended_use": self.spec.intended_use,
            "models": self.spec.models,
            "datasets": self.spec.datasets,
            "acceptance": self.spec.acceptance,
            "monitoring": self.spec.monitoring,
            "signoff": self.spec.signoff,
            "gamp": self.spec.gamp,
            "generated_at": self.generated_at,
            "doc_id": self.doc_id,
            "doc_type": self.doc_type.value if self.doc_type else "",
            "document_title": self.document_title,
            "version": self.version,
            "tool_version": self.tool_version,
            "assessment": self.assessment,
            "requirements": self.requirements,
            "user_requirements": self.user_requirements,
            "functional_requirements": self.functional_requirements,
            "regulatory_requirements": self.regulatory_requirements,
            "risks": self.risks,
            "tests": self.tests,
            "iq_tests": self.tests_for("IQ"),
            "oq_tests": self.tests_for("OQ"),
            "pq_tests": self.tests_for("PQ"),
            "executions": self.executions,
            "ctx": self,
            "run": self.run,
            "runs": self.runs,
            "metrics": self.metrics,
            "metric_results": self.metric_results,
            "calibration": self.run.calibration if self.run else None,
            "evidence": self.evidence,
            "deviations": self.all_deviations,
            "change_controls": self.change_controls,
            "change_control": self.change_control,
            "drift_alerts": self.drift_alerts,
            "periodic_review": self.periodic_review,
            "dataset_summary": self.dataset_summary,
            "redteam_summary": self.redteam_summary,
            "rtm_rows": self.rtm_rows,
            "rtm_markdown": self.rtm_markdown if self.rtm_rows else "",
            "coverage": self.coverage,
            "trace_validation": self.trace_validation,
            "documents": self.documents,
            "signature_block": self.signature_block,
            "critical_metrics_passed": self.critical_metrics_passed,
        }


def build_context(
    spec: AgentSpec,
    doc_type: DocumentType,
    *,
    clock: Clock | None = None,
    doc_id: str = "",
    version: str = "1.0",
    **components: Any,
) -> DocumentContext:
    """Assemble and check the context for one document type."""
    clock = clock or SystemClock()
    context = DocumentContext(
        spec=spec,
        generated_at=clock.now_iso(),
        doc_id=doc_id,
        doc_type=doc_type,
        version=version,
    )
    known = {f for f in vars(context)}
    for name, value in components.items():
        if name not in known:
            raise DocumentError(
                f"unknown context component {name!r} for {doc_type.value}; expected one of "
                f"{', '.join(sorted(known))}"
            )
        if value is not None:
            setattr(context, name, value)

    _check_required(context, doc_type)
    return context


def _check_required(context: DocumentContext, doc_type: DocumentType) -> None:
    missing = []
    for name in REQUIRED_FIELDS.get(doc_type, ()):
        value = getattr(context, name, None)
        if value is None or (isinstance(value, (list, tuple)) and not value):
            missing.append(name)
    if missing:
        raise DocumentError(
            f"cannot generate a {doc_type.value}: the context is missing "
            f"{', '.join(missing)}. A validation document with a missing acceptance "
            f"result would be signed as though it were complete, so generation stops "
            f"here rather than rendering a gap."
        )
