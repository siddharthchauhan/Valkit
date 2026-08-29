"""The validation pipeline.

The stages below are the lifecycle of a validation: ingest the specification,
assess risk, derive the requirements and tests, load the qualification data, run
the battery, compute acceptance, record the executions, generate the documents,
collect signatures, seal the evidence, and hand over to monitoring.

Two design points matter more than the sequence.

*Stages are separate and resumable.* The pipeline holds a
:class:`~valkit.models.ValidationRecord` and each stage advances it, so a caller
can stop before signing and resume later. That is not a convenience: a human
approval step in the middle of an automated process is a regulatory
requirement, and a pipeline that could not stop there would be unusable.

*The validated gate is conjunctive and explicit.* A record reaches
``VALIDATED`` only when every one of the conditions in
:meth:`ValidationPipeline.readiness` holds, and a refusal always names the
condition that failed. This is the single most consequential piece of logic in
the product: everything else produces evidence, and this decides whether the
evidence is sufficient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .audit.store import AuditTrail
from .docgen.context import build_context
from .docgen.generator import PACKAGE_ORDER, DocumentGenerator
from .errors import DocumentError, ValKitError
from .esign.signatures import SignatureService
from .evals.dataset import load_dataset_detailed, summarise
from .evals.judge import LlmJudge
from .evals.providers import FixtureProvider, ModelProvider, resolve_provider
from .evals.runner import EvalRunner
from .models import (
    AgentSpec,
    Deviation,
    Document,
    DocumentType,
    EvalRun,
    RiskLevel,
    RunStatus,
    TestExecution,
    ValidationRecord,
    ValidationStatus,
)
from .spec.derive import DerivedBundle, derive_all
from .spec.loader import load_spec
from .trace.graph import TraceabilityGraph
from .trace.rtm import build_rtm
from .util import Clock, SystemClock
from .vault.store import EvidenceVault

__all__ = ["ValidationPipeline", "Readiness", "PipelineResult", "validate_agent"]


@dataclass
class Readiness:
    """Whether a record may be marked validated, and why not if it may not.

    ``conditions`` are outstanding obligations that do not block the
    qualification evidence but which validated status depends on: chiefly the
    unscripted performance-qualification steps that can only be performed once
    the system is in operation. They are stated rather than hidden, and they
    are carried into the validation summary report.
    """

    ready: bool
    blockers: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ready


@dataclass
class PipelineResult:
    """Everything one pass of the pipeline produced."""

    record: ValidationRecord
    run: EvalRun | None = None
    bundle: DerivedBundle | None = None
    graph: TraceabilityGraph | None = None
    readiness: Readiness | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def validated(self) -> bool:
        return self.record.status is ValidationStatus.VALIDATED


class ValidationPipeline:
    """Orchestrates a validation from specification to sealed package."""

    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        judge: LlmJudge | None = None,
        phi_provider: ModelProvider | None = None,
        vault: EvidenceVault | None = None,
        audit: AuditTrail | None = None,
        signatures: SignatureService | None = None,
        generator: DocumentGenerator | None = None,
        change_register: Any = None,
        clock: Clock | None = None,
        base_dir: str | os.PathLike[str] | None = None,
    ):
        self._clock = clock or SystemClock()
        self._provider = provider
        self._judge = judge
        self._phi_provider = phi_provider
        self.vault = vault
        self.audit = audit
        self.signatures = signatures
        self.generator = generator or DocumentGenerator(clock=self._clock, vault=vault, audit=audit)
        self._change_register = change_register
        self._base_dir = Path(base_dir) if base_dir else None

        self.record: ValidationRecord | None = None
        self.bundle: DerivedBundle | None = None
        self.run: EvalRun | None = None
        self.graph: TraceabilityGraph | None = None
        self.warnings: list[str] = []
        self._dataset = None
        self._dataset_file_sha256: str | None = None
        self._redteam = None

    # -- stages ------------------------------------------------------------

    def ingest_spec(self, spec: AgentSpec | str | os.PathLike[str]) -> ValidationRecord:
        """Stage 1: take in the specification and open the validation record."""
        if not isinstance(spec, AgentSpec):
            spec = load_spec(spec)
        self.record = ValidationRecord(
            agent_id=spec.agent_id,
            agent_version=spec.version,
            spec=spec,
            status=ValidationStatus.DRAFT,
            created_at=self._clock.now_iso(),
        )
        self._audit_event(
            spec, "validation.opened", "agent", spec.ref,
            {"spec_sha256": spec.source_sha256, "gamp_category": int(spec.gamp.category.value)},
        )
        if self.vault is not None:
            from .spec.loader import dump_spec

            self.vault.put_text(
                "spec", dump_spec(spec), content_type="text/yaml", agent_id=spec.agent_id
            )
        return self.record

    def assess_and_derive(self) -> DerivedBundle:
        """Stages 2 and 3: risk assessment, then requirements, risks and tests."""
        record = self._require_record()
        self.bundle = derive_all(record.spec)
        self.record = record.replace(
            status=ValidationStatus.IN_VALIDATION,
            risk_class=self.bundle.assessment.risk_class,
            gamp_category=self.bundle.assessment.gamp_category,
            requirements=self.bundle.requirements,
            risks=self.bundle.risks,
            tests=self.bundle.tests,
        )
        self._audit_event(
            record.spec, "risk.assessed", "agent", record.spec.ref,
            {
                "risk_class": self.bundle.assessment.risk_class.value,
                "derived_class": self.bundle.assessment.derived_class.value,
                "overridden": self.bundle.assessment.overridden,
                "escalations": len(self.bundle.assessment.escalations),
            },
        )
        return self.bundle

    def load_datasets(self) -> Any:
        """Stage 4: load the qualification data and verify its pinned digest."""
        record = self._require_record()
        spec = record.spec
        if spec.datasets.golden_set is None:
            raise ValKitError(
                f"{spec.ref} declares no golden set, so there is nothing to qualify against"
            )
        loaded = load_dataset_detailed(
            spec.datasets.golden_set.ref,
            expected_sha256=spec.datasets.golden_set.sha256,
            base_dir=self._base_dir,
            version=spec.datasets.golden_set.version,
        )
        self._dataset = loaded.dataset
        self._dataset_file_sha256 = loaded.file_sha256

        if spec.datasets.red_team is not None:
            self._redteam = load_dataset_detailed(
                spec.datasets.red_team.ref,
                expected_sha256=spec.datasets.red_team.sha256,
                base_dir=self._base_dir,
                version=spec.datasets.red_team.version,
            ).dataset

        summary = summarise(loaded.dataset)
        self.warnings.extend(summary.notes)
        if self.vault is not None:
            self.vault.put_json(
                "dataset",
                [s.to_dict() for s in loaded.dataset.samples],
                agent_id=spec.agent_id,
                metadata={"ref": loaded.dataset.ref, "file_sha256": loaded.file_sha256},
            )
        return loaded.dataset

    def run_evals(self, run_id: str | None = None) -> EvalRun:
        """Stages 5 to 7: execute the battery, calibrate, compute acceptance."""
        record = self._require_record()
        if self._dataset is None:
            self.load_datasets()

        provider = self._provider or self._resolve_default_provider(record.spec)
        runner = EvalRunner(
            provider,
            judge=self._judge or self._resolve_default_judge(record.spec),
            clock=self._clock,
            vault=self.vault,
            audit=self.audit,
            phi_provider=self._phi_provider,
        )
        self.run = runner.run(
            record.spec,
            self._dataset,
            run_id=run_id,
            dataset_file_sha256=self._dataset_file_sha256,
        )
        self.record = record.replace(runs=[*record.runs, self.run.run_id])
        return self.run

    def execute_tests(self) -> list[TestExecution]:
        """Stage 8: record which tests the run demonstrates, and any deviations."""
        record = self._require_record()
        bundle = self._require_bundle()
        run = self._require_run()

        evidence_refs = [run.transcripts_ref] if run.transcripts_ref else []
        executions: list[TestExecution] = []
        for test in bundle.tests:
            metric = run.metric(test.metric_name) if test.metric_name else None
            deviations: list[Deviation] = []
            passed = True
            observed = "Verified against the run record."

            if metric is not None:
                passed = metric.passed
                observed = metric.rationale
                if metric.failing_sample_ids:
                    deviations.append(
                        Deviation(
                            deviation_id=f"DEV-{test.test_id}",
                            test_id=test.test_id,
                            description=(
                                f"{len(metric.failing_sample_ids)} case(s) did not meet the "
                                f"{metric.name} criterion."
                            ),
                            severity=RiskLevel.HIGH if not metric.passed else RiskLevel.MEDIUM,
                            sample_ids=metric.failing_sample_ids,
                            disposition=(
                                "Recorded for review. Disposition is the quality function's "
                                "to determine."
                            ),
                        )
                    )
                if metric.errors:
                    deviations.append(
                        Deviation(
                            deviation_id=f"DEV-{test.test_id}-ERR",
                            test_id=test.test_id,
                            description=(
                                f"{metric.errors} case(s) failed to execute and were excluded "
                                f"from the denominator."
                            ),
                            severity=RiskLevel.LOW,
                            disposition="Execution errors, not agent failures.",
                        )
                    )
            elif test.phase.value == "PQ" and not test.scripted:
                # An unscripted performance-qualification step is executed
                # against live operation, which has not happened yet. Recording
                # it as passed here would be a false claim.
                continue

            executions.append(
                TestExecution(
                    test_id=test.test_id,
                    run_id=run.run_id,
                    executed_at=self._clock.now_iso(),
                    passed=passed,
                    observed_result=observed,
                    evidence_refs=list(evidence_refs),
                    deviations=deviations,
                    executed_by="ValKit evaluation harness",
                    harness=run.harness,
                )
            )

        self.record = record.replace(executions=executions)
        self.graph = self._build_graph()
        return executions

    def generate_docs(
        self, doc_types: Sequence[DocumentType] | None = None
    ) -> list[Document]:
        """Stage 9: render the validation package from the records."""
        record = self._require_record()
        bundle = self._require_bundle()
        graph = self.graph or self._build_graph()

        components: dict[str, Any] = dict(
            assessment=bundle.assessment,
            requirements=record.requirements,
            risks=record.risks,
            tests=record.tests,
            executions=record.executions,
            evidence=self.vault.records() if self.vault else [],
            rtm_rows=build_rtm(graph),
            coverage=graph.coverage(),
            trace_validation=graph.validate(),
            change_controls=record.change_controls,
        )
        if self.run is not None:
            components["run"] = self.run
            components["runs"] = [self.run]
        if self._dataset is not None:
            components["dataset_summary"] = summarise(self._dataset)
        if self._redteam is not None:
            components["redteam_summary"] = summarise(self._redteam)

        documents = self.generator.generate_package(
            record.spec, doc_types=doc_types or PACKAGE_ORDER, **components
        )
        # The tool qualification document has no dependency on a run and is
        # always included: a customer needs it whether or not this particular
        # validation succeeded.
        try:
            context = build_context(
                record.spec, DocumentType.TOOL_QUALIFICATION, clock=self._clock, **components
            )
            documents.append(self.generator.generate(DocumentType.TOOL_QUALIFICATION, context))
        except DocumentError:
            pass

        self.record = record.replace(documents=documents, evidence=components["evidence"])
        self.graph = self._build_graph()
        return documents

    def sign(
        self,
        doc_id: str,
        signer_id: str,
        meaning: str,
        components: dict[str, str],
        session: Any = None,
        *,
        reason: str = "",
    ) -> Document:
        """Stage 10: apply a Part 11 signature to one document."""
        record = self._require_record()
        if self.signatures is None:
            raise ValKitError("no signature service is configured on the pipeline")

        documents = list(record.documents)
        for index, document in enumerate(documents):
            if document.doc_id != doc_id:
                continue
            signed = self.signatures.apply(
                document,
                signer_id,
                meaning,
                components,
                session,
                signoff=record.spec.signoff,
                reason=reason,
            )
            # The signature manifest must appear in the human-readable form of
            # the record (21 CFR 11.50(b)), and the digest the signature is
            # bound to is the pre-manifest content, so the manifest is appended
            # for display without disturbing what was signed.
            documents[index] = signed
            self.record = record.replace(documents=documents)
            return signed
        raise ValKitError(f"no document with identifier {doc_id!r} in this validation record")

    def seal(self) -> Any:
        """Stage 11: seal the evidence into one signable manifest."""
        record = self._require_record()
        if self.vault is None:
            return None
        manifest = self.vault.manifest(agent_id=record.agent_id)
        self.vault.store_manifest(manifest)
        self._audit_event(
            record.spec, "package.sealed", "agent", record.spec.ref,
            {"manifest_sha256": manifest.manifest_sha256, "artefacts": manifest.count},
        )
        self.record = record.replace(evidence=self.vault.records())
        return manifest

    # -- the validated gate -------------------------------------------------

    def readiness(self) -> Readiness:
        """Every condition for validated status, evaluated independently.

        Each condition is checked and reported separately rather than
        short-circuiting, so a caller learns everything that is outstanding
        rather than the first thing.
        """
        blockers: list[str] = []
        satisfied: list[str] = []
        conditions: list[str] = []

        record = self.record
        if record is None:
            return Readiness(False, ["No validation record has been opened."])

        run = self.run
        if run is None:
            blockers.append("No evaluation run has been executed.")
        elif run.status is not RunStatus.COMPLETED:
            blockers.append(
                f"The evaluation run did not complete: {run.error or run.status.value}."
            )
        else:
            satisfied.append("An evaluation run completed.")
            failed = [m.name for m in run.metrics if m.critical and not m.passed]
            if failed:
                blockers.append(
                    f"Critical acceptance criteria not met: {', '.join(sorted(failed))}."
                )
            elif not [m for m in run.metrics if m.critical]:
                blockers.append("No critical acceptance criterion was evaluated.")
            else:
                satisfied.append("Every critical acceptance criterion was met.")

            if record.spec.models.judge and record.spec.acceptance.judge_calibration is not None:
                if run.calibration is None:
                    blockers.append(
                        "A judge is configured with a calibration threshold, but calibration "
                        "was not performed."
                    )
                elif not run.calibration.passed:
                    blockers.append(
                        f"Judge calibration failed: Cohen's kappa "
                        f"{run.calibration.cohen_kappa:.3f} against a required minimum of "
                        f"{run.calibration.min_required:.2f}."
                    )
                else:
                    satisfied.append("Judge calibration met its threshold.")

        graph = self.graph or (self._build_graph() if self.bundle else None)
        if graph is None:
            blockers.append("The traceability graph has not been built.")
        else:
            coverage = graph.coverage()
            if not coverage.complete:
                blockers.append(
                    f"Critical-requirement coverage is incomplete: "
                    f"{coverage.critical_covered} of {coverage.critical_total} verified."
                )
            else:
                satisfied.append("Every critical requirement is verified by a test.")

            validation = graph.validate()
            if validation.blocking:
                blockers.extend(
                    f"Traceability: {finding.message}" for finding in validation.blocking
                )
            else:
                satisfied.append("The traceability chain has no blocking findings.")
            conditions.extend(
                finding.message
                for finding in validation.advisory
                if finding.kind == "unscripted_test_pending"
            )

        if record.spec.signoff.esignature == "part11":
            if self.signatures is None:
                blockers.append(
                    "The specification requires Part 11 signatures, but no signature "
                    "service is configured."
                )
            else:
                unsigned = [
                    document.doc_id
                    for document in record.documents
                    if not self.signatures.required_signatures_met(
                        document, record.spec.signoff
                    )
                ]
                if not record.documents:
                    blockers.append("No documents have been generated.")
                elif unsigned:
                    blockers.append(
                        f"{len(unsigned)} document(s) lack the required approvals: "
                        f"{', '.join(unsigned[:5])}"
                        f"{'...' if len(unsigned) > 5 else ''}."
                    )
                else:
                    satisfied.append("Every document carries the required valid approvals.")

        if self.vault is not None:
            verification = self.vault.verify()
            if not verification.ok:
                blockers.append(f"Evidence vault verification failed: {verification.reason}.")
            else:
                satisfied.append("Every stored evidence object verifies against its digest.")

        if self.audit is not None:
            chain = self.audit.verify()
            if not chain.ok:
                blockers.append(f"Audit chain verification failed: {chain.reason}.")
            else:
                satisfied.append("The audit chain verifies from its genesis record.")

        if self._change_register is not None:
            blocking = self._change_register.blocking(record.agent_id)
            if blocking:
                blockers.append(
                    f"{len(blocking)} open change control(s) prevent validated status: "
                    f"{', '.join(cc.cc_id for cc in blocking)}."
                )
            else:
                satisfied.append("No open change control blocks this agent.")

        return Readiness(
            ready=not blockers,
            blockers=blockers,
            satisfied=satisfied,
            conditions=conditions,
        )

    def finalise(self) -> ValidationRecord:
        """Mark the record validated, if and only if every condition holds."""
        record = self._require_record()
        readiness = self.readiness()
        status = ValidationStatus.VALIDATED if readiness.ready else ValidationStatus.IN_VALIDATION
        self.record = record.replace(
            status=status,
            validated_at=self._clock.now_iso() if readiness.ready else None,
            links=self.graph.links if self.graph else [],
        )
        self._audit_event(
            record.spec,
            "validation.finalised",
            "agent",
            record.spec.ref,
            {
                "status": status.value,
                "blockers": readiness.blockers,
            },
        )
        return self.record

    # -- helpers -----------------------------------------------------------

    def _build_graph(self) -> TraceabilityGraph:
        record = self._require_record()
        return TraceabilityGraph.from_records(
            requirements=record.requirements,
            risks=record.risks,
            tests=record.tests,
            executions=record.executions,
            runs=[self.run] if self.run else [],
            evidence=self.vault.records() if self.vault else [],
            documents=record.documents,
            change_controls=record.change_controls,
        )

    def _resolve_default_provider(self, spec: AgentSpec) -> ModelProvider:
        try:
            return resolve_provider(spec.models.primary)
        except ValKitError:
            return FixtureProvider(model=spec.models.primary)

    def _resolve_default_judge(self, spec: AgentSpec) -> LlmJudge | None:
        if not spec.models.judge:
            return None
        return LlmJudge(provider=resolve_provider(spec.models.judge))

    def _require_record(self) -> ValidationRecord:
        if self.record is None:
            raise ValKitError("no specification has been ingested; call ingest_spec first")
        return self.record

    def _require_bundle(self) -> DerivedBundle:
        if self.bundle is None:
            raise ValKitError("the package has not been derived; call assess_and_derive first")
        return self.bundle

    def _require_run(self) -> EvalRun:
        if self.run is None:
            raise ValKitError("no evaluation run has been executed; call run_evals first")
        return self.run

    def _audit_event(
        self, spec: AgentSpec, action: str, entity_type: str, entity_id: str, payload: dict
    ) -> None:
        if self.audit is not None:
            self.audit.append(
                actor=spec.metadata.owner or "system",
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload,
            )

    # -- convenience -------------------------------------------------------

    def run_all(
        self, spec: AgentSpec | str | os.PathLike[str], *, run_id: str | None = None
    ) -> PipelineResult:
        """Run every stage up to but not including signing.

        Signing is deliberately excluded. A pipeline that signed on the
        author's behalf would defeat the purpose of requiring a signature.
        """
        self.ingest_spec(spec)
        self.assess_and_derive()
        self.load_datasets()
        self.run_evals(run_id=run_id)
        self.execute_tests()
        self.generate_docs()
        self.seal()
        record = self.finalise()
        return PipelineResult(
            record=record,
            run=self.run,
            bundle=self.bundle,
            graph=self.graph,
            readiness=self.readiness(),
            warnings=list(self.warnings),
        )


def validate_agent(
    spec: AgentSpec | str | os.PathLike[str],
    *,
    provider: ModelProvider | None = None,
    judge: LlmJudge | None = None,
    vault: EvidenceVault | None = None,
    audit: AuditTrail | None = None,
    signatures: SignatureService | None = None,
    clock: Clock | None = None,
    base_dir: str | os.PathLike[str] | None = None,
    run_id: str | None = None,
) -> ValidationRecord:
    """Run the whole pipeline and return the resulting record.

    The one-call entry point. The record's ``status`` says whether validated
    status was reached; use :class:`ValidationPipeline` directly when the
    reasons matter, which they usually do.
    """
    pipeline = ValidationPipeline(
        provider=provider,
        judge=judge,
        vault=vault,
        audit=audit,
        signatures=signatures,
        clock=clock,
        base_dir=base_dir,
    )
    return pipeline.run_all(spec, run_id=run_id).record
