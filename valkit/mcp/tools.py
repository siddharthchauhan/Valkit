"""The MCP tool surface, independent of transport.

ValKit is MCP-native by design. The point is that an engineer's own coding agent
can drive a validation from inside their editor, so producing the evidence is
part of building the agent rather than a separate project afterwards.

The registry here is transport-independent so that the tools are testable
without the MCP package installed, and reusable by the CLI and the HTTP API.
Adding a tool requires no change to the server.

One rule is load-bearing and enforced by test: **a credential passed to
``valkit.sign`` is never echoed in a tool result, never logged, and never
written to the audit payload.** A tool result is model context and may be
persisted in a transcript, which makes this a real exposure rather than a
theoretical one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import ValKitError
from ..util import Clock, SystemClock

__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "ValKitToolContext",
    "build_registry",
    "validate_against_schema",
    "SchemaError",
]


class SchemaError(ValKitError):
    """Arguments did not match a tool's declared input schema."""


# --------------------------------------------------------------------------
# A small JSON Schema subset
# --------------------------------------------------------------------------


def validate_against_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Validate against the JSON Schema subset the tool definitions use.

    Written rather than imported because the subset is small — objects,
    required properties, primitive types, enums, arrays, numeric ranges — and a
    schema validator is not worth a dependency in a package whose whole argument
    is a small supplier surface.
    """
    expected = schema.get("type")

    if expected == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected an object, got {_name(value)}")
        for name in schema.get("required", []):
            if name not in value or value[name] is None:
                raise SchemaError(f"{path}.{name} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaError(
                    f"{path}: unknown argument(s) {', '.join(unknown)}; expected one of "
                    f"{', '.join(sorted(properties))}"
                )
        for name, sub_schema in properties.items():
            if name in value and value[name] is not None:
                validate_against_schema(value[name], sub_schema, f"{path}.{name}")
        return

    if expected == "array":
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"{path}: expected an array, got {_name(value)}")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_against_schema(item, item_schema, f"{path}[{index}]")
        return

    if expected == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: expected a string, got {_name(value)}")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"{path}: expected an integer, got {_name(value)}")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"{path}: expected a number, got {_name(value)}")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: expected a boolean, got {_name(value)}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(
            f"{path}: expected one of {', '.join(repr(v) for v in schema['enum'])}, "
            f"got {value!r}"
        )
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaError(f"{path}: must be at least {schema['minimum']}, got {value}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaError(f"{path}: must be at most {schema['maximum']}, got {value}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        raise SchemaError(
            f"{path}: must be greater than {schema['exclusiveMinimum']}, got {value}"
        )
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        raise SchemaError(f"{path}: must be less than {schema['exclusiveMaximum']}, got {value}")


def _name(value: Any) -> str:
    return {
        dict: "an object",
        list: "an array",
        str: "a string",
        bool: "a boolean",
        int: "an integer",
        float: "a number",
        type(None): "null",
    }.get(type(value), type(value).__name__)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[["ValKitToolContext", dict[str, Any]], dict[str, Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise ValKitError(
                f"unknown tool {name!r}; available tools are {', '.join(sorted(self._tools))}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in self.names()]

    def call(
        self, name: str, arguments: dict[str, Any], context: "ValKitToolContext"
    ) -> dict[str, Any]:
        """Validate the arguments, then dispatch.

        A schema violation returns a structured error rather than raising, so a
        model receives something it can act on rather than a traceback.
        """
        try:
            tool = self.get(name)
        except ValKitError as error:
            return {"error": str(error), "error_type": "unknown_tool"}

        try:
            validate_against_schema(arguments, tool.input_schema)
        except SchemaError as error:
            return {"error": str(error), "error_type": "invalid_arguments"}

        try:
            return tool.handler(context, arguments)
        except ValKitError as error:
            return {"error": str(error), "error_type": type(error).__name__}


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


@dataclass
class ValKitToolContext:
    """The collaborators the handlers need, so they stay pure functions of input.

    Everything is optional and can be built entirely in memory, which is how the
    tools are tested.
    """

    clock: Clock = field(default_factory=SystemClock)
    provider: Any = None
    judge: Any = None
    vault: Any = None
    audit: Any = None
    signatures: Any = None
    generator: Any = None
    monitor: Any = None
    change_register: Any = None
    base_dir: Any = None

    specs: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, Any] = field(default_factory=dict)
    bundles: dict[str, Any] = field(default_factory=dict)
    datasets: dict[str, Any] = field(default_factory=dict)
    executions: dict[str, Any] = field(default_factory=dict)

    def require_spec(self, agent_id: str, version: str | None = None) -> Any:
        key = f"{agent_id}@{version}" if version else agent_id
        for candidate in (key, agent_id):
            if candidate in self.specs:
                return self.specs[candidate]
        raise ValKitError(
            f"no specification for {key!r} has been ingested; call valkit.ingest_spec first"
        )

    def require_run(self, run_id: str) -> Any:
        if run_id not in self.runs:
            raise ValKitError(f"no run with identifier {run_id!r}")
        return self.runs[run_id]


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _ingest_spec(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..spec.derive import derive_all
    from ..spec.loader import parse_spec

    result = parse_spec(arguments["yaml"], "<mcp>", strict=arguments.get("strict", True))
    bundle = derive_all(result.spec)

    context.specs[result.spec.ref] = result.spec
    context.specs[result.spec.agent_id] = result.spec
    context.bundles[result.spec.ref] = bundle

    if context.audit is not None:
        context.audit.append(
            actor=arguments.get("actor", "mcp"),
            action="spec.ingested",
            entity_type="agent",
            entity_id=result.spec.ref,
            payload={"spec_sha256": result.spec.source_sha256},
        )

    return {
        "agent_id": result.spec.agent_id,
        "version": result.spec.version,
        "risk_class": bundle.assessment.risk_class.value,
        "derived_risk_class": bundle.assessment.derived_class.value,
        "gamp_category": int(result.spec.gamp.category.value),
        "requirements": len(bundle.requirements),
        "risks": len(bundle.risks),
        "tests": len(bundle.tests),
        "spec_sha256": result.spec.source_sha256,
        "warnings": result.warnings,
    }


def _run_evals(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..evals.dataset import load_dataset_detailed
    from ..evals.providers import judge_for_spec, provider_for_spec
    from ..evals.runner import EvalRunner

    spec = context.require_spec(arguments["agent_id"], arguments.get("version"))
    dataset_ref = arguments.get("dataset_ref") or (
        spec.datasets.golden_set.ref if spec.datasets.golden_set else None
    )
    if not dataset_ref:
        raise ValKitError(
            f"{spec.ref} declares no golden set and none was supplied, so there is "
            "nothing to evaluate against"
        )

    pinned = spec.datasets.golden_set.sha256 if spec.datasets.golden_set else None
    loaded = load_dataset_detailed(
        dataset_ref, expected_sha256=pinned, base_dir=context.base_dir
    )

    # Falling back to a fixture for a specification that named a hosted model
    # would record a run against a model that was never called, so the fallback
    # resolves what the specification asked for and fails loudly if it cannot.
    provider = context.provider or provider_for_spec(spec, dataset=loaded.dataset)
    runner = EvalRunner(
        provider,
        judge=context.judge if context.judge is not None else judge_for_spec(spec),
        clock=context.clock,
        vault=context.vault,
        audit=context.audit,
    )
    run = runner.run(spec, loaded.dataset, dataset_file_sha256=loaded.file_sha256)
    context.runs[run.run_id] = run
    context.datasets[run.run_id] = loaded.dataset

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "passed": run.passed,
        "dataset_sha256": run.dataset_sha256,
        "transcripts_ref": run.transcripts_ref,
        "metrics": [
            {
                "name": m.name,
                "k": m.k,
                "n": m.n,
                "point_estimate": m.point_estimate,
                "lower_bound": m.lower_bound,
                "target": m.target,
                "method": m.method.value,
                "passed": m.passed,
                "critical": m.critical,
            }
            for m in run.metrics
        ],
        "calibration": (
            {
                "cohen_kappa": run.calibration.cohen_kappa,
                "n": run.calibration.n,
                "passed": run.calibration.passed,
            }
            if run.calibration
            else None
        ),
    }


def _compute_acceptance(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..models import BoundMethod, MetricSpec, MetricType
    from ..stats.acceptance import evaluate_metric, shortfall

    run = context.require_run(arguments["run_id"])
    metric_name = arguments["metric"]

    existing = next((m for m in run.metrics if m.name == metric_name), None)
    scorer = arguments.get("scorer")
    if scorer is None and existing is None:
        raise ValKitError(
            f"run {run.run_id} has no metric named {metric_name!r}, and no scorer was "
            "supplied to compute one"
        )

    spec = MetricSpec(
        name=metric_name,
        type=MetricType.PROPORTION,
        target=arguments["target"],
        confidence=arguments.get("confidence", 0.95),
        method=BoundMethod(arguments.get("method", "clopper_pearson_lower")),
        scorer=scorer,
    )
    result = evaluate_metric(spec, run.samples)

    return {
        "pass": result.passed,
        "lower_bound": result.lower_bound,
        "point_estimate": result.point_estimate,
        "n": result.n,
        "k": result.k,
        "failures": result.failures,
        "errors": result.errors,
        "method": result.method.value,
        "rationale": result.rationale,
        "further_passes_needed": shortfall(result.k, result.n, spec) if not result.passed else 0,
    }


def _generate_docs(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..docgen.generator import DocumentGenerator
    from ..evals.dataset import summarise
    from ..models import DocumentType
    from ..pipeline import derive_executions
    from ..trace.graph import TraceabilityGraph
    from ..trace.rtm import build_rtm

    run = context.require_run(arguments["run_id"])
    spec = context.require_spec(run.agent_id, run.agent_version)
    bundle = context.bundles.get(spec.ref)
    if bundle is None:
        from ..spec.derive import derive_all

        bundle = derive_all(spec)
        context.bundles[spec.ref] = bundle

    # The same derivation the pipeline uses, so a qualification report produced
    # through a tool call is the document the pipeline would have produced.
    executions = derive_executions(bundle.tests, run, clock=context.clock)
    context.executions[run.run_id] = executions

    graph = TraceabilityGraph.from_records(
        requirements=bundle.requirements,
        risks=bundle.risks,
        tests=bundle.tests,
        executions=executions,
        runs=[run],
        evidence=context.vault.records() if context.vault else (),
    )
    generator = context.generator or DocumentGenerator(
        clock=context.clock, vault=context.vault, audit=context.audit
    )
    requested = arguments.get("doc_types")
    doc_types = [DocumentType(t) for t in requested] if requested else None

    components: dict[str, Any] = dict(
        assessment=bundle.assessment,
        requirements=bundle.requirements,
        risks=bundle.risks,
        tests=bundle.tests,
        executions=executions,
        run=run,
        runs=[run],
        evidence=context.vault.records() if context.vault else [],
        rtm_rows=build_rtm(graph),
        coverage=graph.coverage(),
        trace_validation=graph.validate(),
    )
    dataset = context.datasets.get(run.run_id)
    if dataset is not None:
        components["dataset_summary"] = summarise(dataset)

    documents = generator.generate_package(spec, doc_types=doc_types, **components)
    for document in documents:
        context.documents[document.doc_id] = document

    return {
        "doc_ids": [d.doc_id for d in documents],
        "documents": [
            {
                "doc_id": d.doc_id,
                "doc_type": d.doc_type.value,
                "title": d.title,
                "content_sha256": d.content_sha256,
                "status": d.status.value,
            }
            for d in documents
        ],
        "skipped": generator.skipped(),
    }


def _sign(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    if context.signatures is None:
        raise ValKitError("no signature service is configured on this MCP context")

    doc_id = arguments["doc_id"]
    document = context.documents.get(doc_id)
    if document is None:
        raise ValKitError(f"no document with identifier {doc_id!r}")

    # The components go straight to the signature service. Nothing derived from
    # them appears in the result: a tool result is model context and may be
    # persisted in a transcript.
    signature = context.signatures.sign(
        document,
        arguments["user"],
        arguments["meaning"],
        arguments["components"],
        reason=arguments.get("reason", ""),
    )
    context.documents[doc_id] = document.replace(
        signatures=[*document.signatures, signature]
    )

    return {
        "signature_id": signature.signature_id,
        "document_id": signature.document_id,
        "document_sha256": signature.document_sha256,
        "printed_name": signature.printed_name,
        "signed_at": signature.signed_at,
        "meaning": signature.meaning.value,
        "components_used": signature.components_used,
        "manifest": context.signatures.manifest(signature),
    }


def _get_drift(context: ValKitToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    from ..models import MonitoringSpec

    if context.monitor is None:
        raise ValKitError("no drift monitor is configured on this MCP context")

    agent_id = arguments["agent_id"]
    window = arguments.get("window", 20)
    metrics = arguments.get("metrics") or context.monitor.store.metrics(agent_id)

    series: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for metric in metrics:
        points = context.monitor.store.series(agent_id, metric)[-window:]
        series.extend(
            {
                "metric": metric,
                "observed_at": p.observed_at,
                "value": p.value,
                "n": p.n,
                "run_id": p.run_id,
            }
            for p in points
        )
        alert = context.monitor.evaluate(agent_id, metric, MonitoringSpec(window=window))
        if alert is not None:
            violations.extend(
                {
                    "metric": metric,
                    "alert_id": alert.alert_id,
                    "rule": v.rule,
                    "severity": v.severity.value,
                    "value": v.value,
                    "description": v.description,
                }
                for v in alert.violations
            )

    return {"agent_id": agent_id, "spc_points": series, "violations": violations}


def _open_change_control(
    context: ValKitToolContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ..models import ChangeTrigger

    if context.change_register is None:
        raise ValKitError("no change control register is configured on this MCP context")

    agent_id = arguments["agent_id"]
    spec = context.specs.get(agent_id)
    record = context.change_register.open(
        agent_id=agent_id,
        agent_version=arguments.get("version", spec.version if spec else ""),
        trigger=ChangeTrigger(arguments.get("trigger", "other")),
        reason=arguments["reason"],
    )
    assessed = context.change_register.assess_impact(
        record.cc_id, spec, metrics=arguments.get("metrics")
    )
    return {
        "cc_id": assessed.cc_id,
        "status": assessed.status.value,
        "trigger": assessed.trigger.value,
        "required_scope": assessed.required_scope,
        "impact": assessed.impact,
    }


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------


def build_registry() -> ToolRegistry:
    """The seven tools that make up ValKit's MCP surface."""
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            name="valkit.ingest_spec",
            description=(
                "Load and validate a valkit.yaml specification, then derive the model "
                "risk assessment, the user and functional requirements, the risk "
                "register and the IQ/OQ/PQ test cases. Use this first: every other tool "
                "needs an ingested specification. Returns the derived risk class and any "
                "warnings about constructs that are legal but weaken the resulting "
                "validation package."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["yaml"],
                "properties": {
                    "yaml": {"type": "string", "description": "The valkit.yaml content."},
                    "strict": {
                        "type": "boolean",
                        "description": "Reject unknown keys (default true).",
                    },
                    "actor": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "version": {"type": "string"},
                    "risk_class": {"type": "string"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
            },
            handler=_ingest_spec,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.run_evals",
            description=(
                "Execute the acceptance battery for an ingested agent against its golden "
                "set, verifying the dataset's pinned digest first. Computes each metric "
                "as a one-sided lower confidence bound and calibrates the judge where one "
                "is configured. Returns the run identifier, which the document and "
                "acceptance tools take."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string"},
                    "version": {"type": "string"},
                    "dataset_ref": {
                        "type": "string",
                        "description": "Overrides the golden set named in the specification.",
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "metrics": {"type": "array"},
                },
            },
            handler=_run_evals,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.compute_acceptance",
            description=(
                "Recompute the acceptance decision for one metric of a completed run "
                "against a different target, confidence or bound method. Useful for "
                "answering 'what target could this run actually support?' without "
                "re-executing it. Returns the bound, the decision, and how many further "
                "passing cases would be needed if it fails."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id", "metric", "target"],
                "properties": {
                    "run_id": {"type": "string"},
                    "metric": {"type": "string"},
                    "target": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 1,
                    },
                    "confidence": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 1,
                    },
                    "method": {
                        "type": "string",
                        "enum": [
                            "clopper_pearson_lower",
                            "wilson_lower",
                            "jeffreys_lower",
                            "wald_lower",
                        ],
                    },
                    "scorer": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "pass": {"type": "boolean"},
                    "lower_bound": {"type": "number"},
                    "n": {"type": "integer"},
                    "failures": {"type": "integer"},
                },
            },
            handler=_compute_acceptance,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.generate_docs",
            description=(
                "Generate the validation document package from a completed run: user and "
                "functional requirements, risk assessment, validation plan, credibility "
                "assessment in the FDA framework's seven steps, IQ/OQ/PQ protocols and "
                "reports, the traceability matrix and the validation summary. Documents "
                "are generated unsigned; use valkit.sign to apply approvals."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "string"},
                    "doc_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Defaults to the whole package.",
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {"doc_ids": {"type": "array", "items": {"type": "string"}}},
            },
            handler=_generate_docs,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.sign",
            description=(
                "Apply a 21 CFR Part 11 electronic signature to a generated document. The "
                "signature binds to the document's content digest, so any later alteration "
                "invalidates it. Credential components are passed through to the signature "
                "service and are never returned, logged or recorded in the audit trail."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["doc_id", "user", "meaning", "components"],
                "properties": {
                    "doc_id": {"type": "string"},
                    "user": {"type": "string"},
                    "meaning": {
                        "type": "string",
                        "enum": [
                            "authored",
                            "reviewed",
                            "approved",
                            "executed",
                            "verified",
                            "rejected",
                        ],
                    },
                    "components": {
                        "type": "object",
                        "description": "Signature components. Never echoed in the result.",
                    },
                    "reason": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "signature_id": {"type": "string"},
                    "manifest": {"type": "string"},
                },
            },
            handler=_sign,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.get_drift",
            description=(
                "Return the monitored metric series for an agent together with any control "
                "rule violations on the most recent point. Use this to answer whether a "
                "validated agent is still performing within the limits its package was "
                "signed against."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string"},
                    "window": {"type": "integer", "minimum": 2},
                    "metrics": {"type": "array", "items": {"type": "string"}},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "spc_points": {"type": "array"},
                    "violations": {"type": "array"},
                },
            },
            handler=_get_drift,
        )
    )

    registry.register(
        ToolDefinition(
            name="valkit.open_change_control",
            description=(
                "Open a change control for an agent and derive the re-evaluation scope its "
                "trigger requires. A model version change requires the full battery and "
                "judge recalibration; a drift alert requires the metric that tripped. The "
                "change cannot be approved until a run covering that scope has passed."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["agent_id", "reason"],
                "properties": {
                    "agent_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "version": {"type": "string"},
                    "trigger": {
                        "type": "string",
                        "enum": [
                            "model_version",
                            "prompt_change",
                            "dataset_change",
                            "spec_change",
                            "drift",
                            "periodic_review",
                            "defect",
                            "other",
                        ],
                    },
                    "metrics": {"type": "array", "items": {"type": "string"}},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "cc_id": {"type": "string"},
                    "required_scope": {"type": "array", "items": {"type": "string"}},
                },
            },
            handler=_open_change_control,
        )
    )

    return registry
