"""Deterministic factories for tests and demos.

Every factory here is pure: given the same arguments it returns byte-identical
records, so digests and generated documents can be asserted exactly. Nothing
in this module touches the network, the clock or the filesystem.

This module depends only on :mod:`valkit.models` and :mod:`valkit.util`, so it
is safe to import from any test regardless of which subsystems are installed.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .models import (
    AcceptanceSpec,
    AgentSpec,
    BoundMethod,
    ContextOfUse,
    Dataset,
    DatasetSpec,
    DatasetsSpec,
    Document,
    DocumentType,
    EvalRun,
    GampCategory,
    GampSpec,
    GoldenSample,
    HarnessInfo,
    IntendedUse,
    JudgeCalibrationSpec,
    MetricSpec,
    MetricType,
    ModelsSpec,
    MonitoringSpec,
    QualificationPhase,
    Requirement,
    RequirementKind,
    Risk,
    RiskClass,
    RiskLevel,
    RunStatus,
    SampleResult,
    Score,
    SignoffSpec,
    SpecMetadata,
    TestCase,
)
from .util import sha256_obj, sha256_text

__all__ = [
    "make_spec",
    "make_dataset",
    "make_samples",
    "make_sample_results",
    "make_run",
    "make_requirements",
    "make_risks",
    "make_tests",
    "make_document",
    "EXAMPLE_YAML",
]


EXAMPLE_YAML = """\
apiVersion: valkit/v1
kind: AgentValidation
metadata:
  agent_id: rave-als-generator
  version: 2.3.1
  owner: fde@sponsor.com
  system_of_record: Medidata Rave EDC
context_of_use:
  question_of_interest: >
    Does the agent correctly generate Archival Listing Specification (ALS)
    entries from a study protocol so that EDC build is accurate?
  role: "Assistive; human reviews every ALS before load (human-in-the-loop)."
  regulatory_impact: medium
  decision_consequence: medium
  model_influence: medium
intended_use:
  in_scope:
    - ALS field/form generation
    - edit-check suggestion
  out_of_scope:
    - auto-loading to production without review
gamp:
  category: 5
  risk_class: high
models:
  primary: bedrock/anthropic.claude-sonnet-4
  judge: bedrock/anthropic.claude-opus-4
  phi_safe_local: ollama/llama3.1:70b
datasets:
  golden_set: s3://valkit/rave-als/golden_v7.jsonl
  red_team: s3://valkit/rave-als/redteam_v3.jsonl
acceptance:
  metrics:
    - name: field_accuracy
      type: proportion
      target: 0.98
      confidence: 0.95
      method: clopper_pearson_lower
    - name: citation_accuracy
      type: proportion
      target: 0.95
      confidence: 0.95
      method: wilson_lower
    - name: numeric_tolerance
      type: numeric_tolerance
      tolerance_abs: 0.001
      target: 0.99
  judge_calibration:
    min_cohen_kappa: 0.80
monitoring:
  schedule: "0 6 * * 1"
  spc_rule: western_electric
  alert_channels:
    - slack
    - jira
signoff:
  approvers:
    - qa_lead
    - csv_lead
  esignature: part11
"""


def make_spec(
    agent_id: str = "rave-als-generator",
    version: str = "2.3.1",
    *,
    risk_class: RiskClass | None = RiskClass.HIGH,
    category: GampCategory = GampCategory.BESPOKE,
    metrics: Sequence[MetricSpec] | None = None,
) -> AgentSpec:
    """A representative, fully populated specification."""
    if metrics is None:
        metrics = [
            MetricSpec(
                name="field_accuracy",
                type=MetricType.PROPORTION,
                target=0.98,
                confidence=0.95,
            ),
            MetricSpec(
                name="citation_accuracy",
                type=MetricType.PROPORTION,
                target=0.95,
                confidence=0.95,
                method=BoundMethod.WILSON_LOWER,
            ),
        ]
    spec = AgentSpec(
        metadata=SpecMetadata(
            agent_id=agent_id,
            version=version,
            owner="fde@sponsor.com",
            system_of_record="Medidata Rave EDC",
        ),
        context_of_use=ContextOfUse(
            question_of_interest=(
                "Does the agent correctly generate ALS entries from a study "
                "protocol so that EDC build is accurate?"
            ),
            role="Assistive; a human reviews every ALS before load.",
            model_influence=RiskLevel.MEDIUM,
            decision_consequence=RiskLevel.MEDIUM,
            human_in_the_loop=True,
        ),
        intended_use=IntendedUse(
            in_scope=["ALS field/form generation", "edit-check suggestion"],
            out_of_scope=["auto-loading to production without review"],
            users=["EDC build engineer", "data manager"],
        ),
        gamp=GampSpec(category=category, risk_class=risk_class),
        models=ModelsSpec(
            primary="fixture/deterministic",
            judge="fixture/judge",
            temperature=0.0,
            seed=0,
        ),
        datasets=DatasetsSpec(
            golden_set=DatasetSpec(ref="golden_v7.jsonl", version="v7"),
            red_team=DatasetSpec(ref="redteam_v3.jsonl", version="v3"),
        ),
        acceptance=AcceptanceSpec(
            metrics=list(metrics),
            judge_calibration=JudgeCalibrationSpec(min_cohen_kappa=0.80, min_samples=10),
        ),
        monitoring=MonitoringSpec(schedule="0 6 * * 1", alert_channels=["slack"]),
        signoff=SignoffSpec(approvers=["qa_lead", "csv_lead"], reviewers=["csv_lead"]),
    )
    return spec.replace(source_sha256=sha256_obj(spec))


def make_samples(n: int = 20, *, phi_every: int = 0, labelled: bool = True) -> list[GoldenSample]:
    """``n`` deterministic golden samples, ids ``S-0001`` upward."""
    samples: list[GoldenSample] = []
    for index in range(1, n + 1):
        samples.append(
            GoldenSample(
                sample_id=f"S-{index:04d}",
                input=f"Generate the ALS entry for protocol section {index}.",
                target=f"FIELD_{index:04d}",
                metadata={"section": str(index), "form": "DM" if index % 2 else "AE"},
                contains_phi=bool(phi_every and index % phi_every == 0),
                stratum="DM" if index % 2 else "AE",
                human_label=1.0 if labelled else None,
            )
        )
    return samples


def make_dataset(
    n: int = 20,
    *,
    name: str = "golden_set",
    ref: str = "golden_v7.jsonl",
    phi_every: int = 0,
) -> Dataset:
    samples = make_samples(n, phi_every=phi_every)
    return Dataset(
        name=name,
        ref=ref,
        sha256=sha256_obj(samples),
        samples=samples,
        version="v7",
    )


def make_sample_results(
    n: int = 20,
    *,
    failures: int = 0,
    scorer: str = "field_accuracy",
    errors: int = 0,
) -> list[SampleResult]:
    """``n`` results where the last ``failures`` samples fail the scorer.

    ``errors`` samples (taken from the front) additionally carry a provider
    error, so callers can exercise error accounting.
    """
    results: list[SampleResult] = []
    for index in range(1, n + 1):
        failed = index > (n - failures)
        errored = index <= errors
        results.append(
            SampleResult(
                sample_id=f"S-{index:04d}",
                output=f"FIELD_{index:04d}" if not failed else "FIELD_WRONG",
                scores={
                    scorer: Score(
                        value=0.0 if failed else 1.0,
                        passed=not failed,
                        scorer=scorer,
                        explanation="deterministic fixture",
                    )
                },
                stratum="DM" if index % 2 else "AE",
                latency_ms=100.0,
                error="provider timeout" if errored else None,
            )
        )
    return results


def make_run(
    spec: AgentSpec | None = None,
    *,
    n: int = 20,
    failures: int = 0,
    run_id: str = "RUN-2026-08-19-004",
    status: RunStatus = RunStatus.COMPLETED,
    scorer: str = "field_accuracy",
) -> EvalRun:
    """A completed run with deterministic ids and no metrics computed yet.

    Metrics are left empty on purpose: the acceptance engine is what fills
    them in, and tests for that engine need an un-scored run to start from.
    """
    spec = spec or make_spec()
    dataset = make_dataset(n)
    return EvalRun(
        run_id=run_id,
        agent_id=spec.agent_id,
        agent_version=spec.version,
        dataset_ref=dataset.ref,
        dataset_sha256=dataset.sha256,
        model=spec.models.primary,
        judge_model=spec.models.judge,
        status=status,
        started_at="2026-08-19T09:00:00Z",
        finished_at="2026-08-19T09:12:00Z",
        spec_sha256=spec.source_sha256,
        seed=0,
        samples=make_sample_results(n, failures=failures, scorer=scorer),
        harness=HarnessInfo(
            name="valkit",
            version="0.1.0",
            provider="fixture",
            config_sha256=sha256_text("fixture-config"),
            python_version="3.11",
            platform="test",
        ),
    )


def make_requirements() -> list[Requirement]:
    return [
        Requirement(
            req_id="URS-01",
            kind=RequirementKind.USER,
            text="The agent shall generate ALS field entries from a study protocol.",
            source="context_of_use.question_of_interest",
        ),
        Requirement(
            req_id="URS-03",
            kind=RequirementKind.USER,
            text="The agent shall cite the protocol section supporting each entry.",
            source="acceptance.metrics.citation_accuracy",
        ),
        Requirement(
            req_id="FRS-07",
            kind=RequirementKind.FUNCTIONAL,
            text="Each generated entry carries a source span referencing the protocol.",
            parent_ids=["URS-03"],
            source="derived",
        ),
    ]


def make_risks() -> list[Risk]:
    return [
        Risk(
            risk_id="RISK-04",
            description="The agent fabricates a protocol citation.",
            failure_mode="hallucinated citation accepted into the EDC build",
            severity=RiskLevel.HIGH,
            probability=RiskLevel.MEDIUM,
            detectability=RiskLevel.MEDIUM,
            risk_class=RiskClass.HIGH,
            mitigation="Citation accuracy acceptance criterion plus human review.",
            requirement_ids=["URS-03", "FRS-07"],
            category="hallucination",
        )
    ]


def make_tests() -> list[TestCase]:
    return [
        TestCase(
            test_id="OQ-014",
            phase=QualificationPhase.OQ,
            title="Citation Accuracy",
            objective=(
                "Verify the agent's cited source spans match the source document "
                "for the approved golden set."
            ),
            acceptance_text=(
                "Citation accuracy proportion, one-sided Wilson 95% lower bound >= 0.95."
            ),
            requirement_ids=["URS-03", "FRS-07"],
            risk_ids=["RISK-04"],
            metric_name="citation_accuracy",
            procedure=[
                "Load the pinned golden set and verify its SHA-256.",
                "Execute the citation_accuracy scorer over every sample.",
                "Record k passes of n and compute the Wilson lower bound.",
            ],
            expected_result="Lower bound >= 0.95 with zero P1 citation fabrications.",
        )
    ]


def make_document(
    doc_type: DocumentType = DocumentType.OQ_PROTOCOL,
    *,
    content: str = "# OQ Protocol\n\nDeterministic fixture body.\n",
    doc_id: str = "DOC-OQ-0001",
    agent_id: str = "rave-als-generator",
    agent_version: str = "2.3.1",
) -> Document:
    return Document(
        doc_id=doc_id,
        doc_type=doc_type,
        title=doc_type.value.replace("_", " ").title(),
        agent_id=agent_id,
        agent_version=agent_version,
        content=content,
        content_sha256=sha256_text(content),
        generated_at="2026-08-19T09:15:00Z",
        template=f"{doc_type.value.lower()}.md.j2",
    )


def scores_from_labels(
    labels: Iterable[float], *, scorer: str = "judge", prefix: str = "S"
) -> list[SampleResult]:
    """Build sample results directly from a sequence of 0/1 judge scores."""
    results: list[SampleResult] = []
    for index, label in enumerate(labels, start=1):
        results.append(
            SampleResult(
                sample_id=f"{prefix}-{index:04d}",
                output="",
                scores={
                    scorer: Score(
                        value=float(label), passed=bool(label), scorer=scorer
                    )
                },
            )
        )
    return results


def _unused(*args: Any) -> None:  # pragma: no cover - keeps linters quiet
    return None
