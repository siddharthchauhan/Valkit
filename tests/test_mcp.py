"""Tests for the MCP tool surface.

The assertion that matters most is the last one: a credential passed to
valkit.sign must not appear in the tool result. A tool result is model context
and may be persisted in a transcript, so a leak there is durable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from valkit.audit import AuditTrail
from valkit.change import ChangeControlRegister
from valkit.docgen import DocumentGenerator
from valkit.drift import DriftMonitor, InMemoryMonitoringStore
from valkit.errors import ValKitError
from valkit.esign import SignatureService, StaticIdentityStore
from valkit.evals import FixtureJudgeProvider, FixtureProvider, LlmJudge, load_dataset
from valkit.mcp.tools import (
    SchemaError,
    ValKitToolContext,
    build_registry,
    validate_against_schema,
)
from valkit.models import DriftPoint
from valkit.testing import EXAMPLE_YAML
from valkit.util import FrozenClock
from valkit.vault import EvidenceVault

ROOT = Path(__file__).resolve().parents[1]
SPEC_YAML = (ROOT / "examples" / "valkit.yaml").read_text()
PASSWORD = "a-strong-signing-password"


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def context(workdir):
    """Everything in memory or a temp directory. No network, no wall clock."""
    clock = FrozenClock(step=1)
    audit = AuditTrail(":memory:", clock)
    identities = StaticIdentityStore(clock)
    identities.add("qa_lead", "Dana Okafor", PASSWORD, roles=["qa"])
    dataset = load_dataset(str(ROOT / "examples" / "datasets" / "rave_als_golden.jsonl"))

    return ValKitToolContext(
        clock=clock,
        provider=FixtureProvider.from_dataset(dataset, model="fixture/rave-als"),
        judge=LlmJudge(provider=FixtureJudgeProvider()),
        vault=EvidenceVault(workdir / "vault", clock),
        audit=audit,
        signatures=SignatureService(identities, clock, audit),
        generator=DocumentGenerator(clock=clock),
        monitor=DriftMonitor(InMemoryMonitoringStore(), clock=clock),
        change_register=ChangeControlRegister(clock=clock, audit=audit),
        base_dir=ROOT,
    )


class TestSchemaValidator:
    def test_accepts_a_valid_object(self):
        schema = {
            "type": "object",
            "required": ["a"],
            "properties": {"a": {"type": "string"}},
        }
        validate_against_schema({"a": "x"}, schema)

    def test_missing_required_property(self):
        schema = {"type": "object", "required": ["a"], "properties": {}}
        with pytest.raises(SchemaError, match=r"arguments\.a is required"):
            validate_against_schema({}, schema)

    def test_null_counts_as_missing(self):
        schema = {"type": "object", "required": ["a"], "properties": {}}
        with pytest.raises(SchemaError, match="is required"):
            validate_against_schema({"a": None}, schema)

    def test_unknown_property_rejected_when_closed(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string"}},
        }
        with pytest.raises(SchemaError, match="unknown argument"):
            validate_against_schema({"a": "x", "b": 1}, schema)

    @pytest.mark.parametrize(
        "schema_type, good, bad",
        [
            ("string", "x", 1),
            ("integer", 1, "x"),
            ("number", 1.5, "x"),
            ("boolean", True, "x"),
            ("array", [1], "x"),
        ],
    )
    def test_primitive_types(self, schema_type, good, bad):
        schema = {"type": schema_type}
        validate_against_schema(good, schema)
        with pytest.raises(SchemaError):
            validate_against_schema(bad, schema)

    def test_booleans_are_not_integers(self):
        """True would otherwise satisfy an integer schema."""
        with pytest.raises(SchemaError):
            validate_against_schema(True, {"type": "integer"})

    def test_enum(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        validate_against_schema("a", schema)
        with pytest.raises(SchemaError, match="expected one of"):
            validate_against_schema("c", schema)

    def test_numeric_bounds(self):
        schema = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}
        validate_against_schema(0.5, schema)
        with pytest.raises(SchemaError, match="must be less than 1"):
            validate_against_schema(1.0, schema)
        with pytest.raises(SchemaError, match="must be greater than 0"):
            validate_against_schema(0.0, schema)

    def test_array_items(self):
        schema = {"type": "array", "items": {"type": "string"}}
        validate_against_schema(["a"], schema)
        with pytest.raises(SchemaError, match=r"arguments\[0\]"):
            validate_against_schema([1], schema)


class TestRegistry:
    def test_the_seven_tools_are_present(self, registry):
        assert registry.names() == [
            "valkit.compute_acceptance",
            "valkit.generate_docs",
            "valkit.get_drift",
            "valkit.ingest_spec",
            "valkit.open_change_control",
            "valkit.run_evals",
            "valkit.sign",
        ]

    def test_every_tool_is_described_for_a_model(self, registry):
        for definition in registry.definitions():
            assert len(definition.description) > 120, definition.name
            assert definition.input_schema["type"] == "object"
            assert definition.output_schema

    def test_unknown_tool_returns_a_structured_error(self, registry, context):
        result = registry.call("valkit.nope", {}, context)
        assert result["error_type"] == "unknown_tool"
        assert "available tools are" in result["error"]

    def test_invalid_arguments_return_a_structured_error(self, registry, context):
        result = registry.call("valkit.ingest_spec", {}, context)
        assert result["error_type"] == "invalid_arguments"
        assert "yaml is required" in result["error"]

    def test_a_valkit_error_becomes_a_structured_error(self, registry, context):
        result = registry.call("valkit.run_evals", {"agent_id": "unknown"}, context)
        assert "error" in result
        assert "ingest_spec first" in result["error"]

    def test_every_result_is_canonically_serialisable(self, registry, context):
        from valkit.util import canonical_json

        result = registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        assert json.loads(canonical_json(result))["agent_id"] == "rave-als-generator"


class TestIngestSpec:
    def test_ingests_and_derives(self, registry, context):
        result = registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        assert result["agent_id"] == "rave-als-generator"
        assert result["risk_class"] == "medium"
        assert result["requirements"] > 0
        assert result["tests"] > 0

    def test_warnings_are_surfaced(self, registry, context):
        result = registry.call("valkit.ingest_spec", {"yaml": EXAMPLE_YAML}, context)
        assert any("not pinned to a digest" in w for w in result["warnings"])

    def test_an_invalid_spec_returns_the_path(self, registry, context):
        result = registry.call(
            "valkit.ingest_spec", {"yaml": "apiVersion: valkit/v1\nkind: AgentValidation\n"},
            context,
        )
        assert "metadata" in result["error"]

    def test_ingestion_is_audited(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        assert context.audit.filter(action="spec.ingested")


class TestRunAndAcceptance:
    def _ingested(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)

    def test_runs_the_battery(self, registry, context):
        self._ingested(registry, context)
        result = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)
        assert result["status"] == "completed"
        assert result["passed"]
        assert {m["name"] for m in result["metrics"]} == {
            "field_accuracy",
            "citation_accuracy",
            "no_invented_identifiers",
        }

    def test_recomputes_acceptance_against_a_different_target(self, registry, context):
        self._ingested(registry, context)
        run = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)

        lenient = registry.call(
            "valkit.compute_acceptance",
            {
                "run_id": run["run_id"],
                "metric": "field_accuracy",
                "target": 0.80,
                "scorer": "exact_match",
            },
            context,
        )
        strict = registry.call(
            "valkit.compute_acceptance",
            {
                "run_id": run["run_id"],
                "metric": "field_accuracy",
                "target": 0.99,
                "scorer": "exact_match",
            },
            context,
        )
        assert lenient["pass"] and not strict["pass"]
        assert strict["further_passes_needed"] > 0

    def test_the_bound_matches_the_statistics_module(self, registry, context):
        from valkit.stats import clopper_pearson_lower

        self._ingested(registry, context)
        run = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)
        result = registry.call(
            "valkit.compute_acceptance",
            {
                "run_id": run["run_id"],
                "metric": "field_accuracy",
                "target": 0.85,
                "scorer": "exact_match",
            },
            context,
        )
        assert result["lower_bound"] == pytest.approx(
            clopper_pearson_lower(result["k"], result["n"], 0.95), abs=1e-12
        )

    def test_an_unknown_run_is_reported(self, registry, context):
        result = registry.call(
            "valkit.compute_acceptance",
            {"run_id": "nope", "metric": "m", "target": 0.9},
            context,
        )
        assert "no run with identifier" in result["error"]


class TestDocumentsAndSigning:
    def _to_documents(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        run = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)
        return registry.call("valkit.generate_docs", {"run_id": run["run_id"]}, context)

    def test_generates_the_package(self, registry, context):
        result = self._to_documents(registry, context)
        assert len(result["doc_ids"]) >= 10
        types = {d["doc_type"] for d in result["documents"]}
        assert "CREDIBILITY_REPORT" in types
        assert "OQ_REPORT" in types

    def test_the_qualification_reports_are_generated(self, registry, context):
        """The reports need executions, which the tool has to derive itself.

        Generating the protocols but not the reports would be a package that
        looks complete and demonstrates nothing.
        """
        result = self._to_documents(registry, context)
        types = {d["doc_type"] for d in result["documents"]}
        assert {"IQ_REPORT", "OQ_REPORT", "PQ_REPORT"} <= types
        assert "OQ_REPORT" not in result["skipped"]

    def test_the_executions_match_the_pipeline(self, registry, context):
        """The two routes to a qualification report must agree.

        A document generated through a tool call and one generated through the
        pipeline are the same regulatory record; if they could differ, the
        evidence would depend on the route taken to produce it.
        """
        from valkit.pipeline import derive_executions

        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        run_result = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)
        run_id = run_result["run_id"]
        registry.call("valkit.generate_docs", {"run_id": run_id}, context)

        spec = context.require_spec("rave-als-generator")
        bundle = context.bundles[spec.ref]
        expected = derive_executions(
            bundle.tests, context.require_run(run_id), clock=FrozenClock(step=1)
        )

        recorded = context.executions[run_id]
        assert [e.test_id for e in recorded] == [e.test_id for e in expected]
        assert [e.passed for e in recorded] == [e.passed for e in expected]
        assert [len(e.deviations) for e in recorded] == [len(e.deviations) for e in expected]

    def test_generates_a_subset_when_asked(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        run = registry.call("valkit.run_evals", {"agent_id": "rave-als-generator"}, context)
        result = registry.call(
            "valkit.generate_docs", {"run_id": run["run_id"], "doc_types": ["VSR"]}, context
        )
        assert [d["doc_type"] for d in result["documents"]] == ["VSR"]

    def test_signs_a_document(self, registry, context):
        documents = self._to_documents(registry, context)
        doc_id = documents["doc_ids"][0]
        result = registry.call(
            "valkit.sign",
            {
                "doc_id": doc_id,
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            context,
        )
        assert result["printed_name"] == "Dana Okafor"
        assert result["meaning"] == "approved"
        assert "Electronic signature" in result["manifest"]

    def test_the_credential_never_appears_in_the_result(self, registry, context):
        """A tool result is model context and may be persisted in a transcript."""
        from valkit.util import canonical_json

        documents = self._to_documents(registry, context)
        result = registry.call(
            "valkit.sign",
            {
                "doc_id": documents["doc_ids"][0],
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            context,
        )
        assert PASSWORD not in canonical_json(result)
        assert PASSWORD not in result["manifest"]
        assert result["components_used"] == ["password", "user_id"]

    def test_the_credential_never_reaches_the_audit_trail(self, registry, context):
        documents = self._to_documents(registry, context)
        registry.call(
            "valkit.sign",
            {
                "doc_id": documents["doc_ids"][0],
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            context,
        )
        assert PASSWORD not in context.audit.export_jsonl()
        assert PASSWORD not in context.audit.export_text()

    def test_a_bad_credential_is_refused_without_echoing_it(self, registry, context):
        documents = self._to_documents(registry, context)
        result = registry.call(
            "valkit.sign",
            {
                "doc_id": documents["doc_ids"][0],
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": "wrong-password"},
            },
            context,
        )
        assert "error" in result
        assert "wrong-password" not in result["error"]

    def test_an_invalid_meaning_is_rejected_by_the_schema(self, registry, context):
        result = registry.call(
            "valkit.sign",
            {
                "doc_id": "x",
                "user": "u",
                "meaning": "blessed",
                "components": {},
            },
            context,
        )
        assert result["error_type"] == "invalid_arguments"
        assert "expected one of" in result["error"]


class TestDriftAndChangeControl:
    def test_drift_returns_the_series(self, registry, context):
        for index, value in enumerate([0.97, 0.98, 0.97, 0.96, 0.80]):
            context.monitor.store.append(
                DriftPoint(
                    agent_id="rave-als-generator",
                    metric="field_accuracy",
                    observed_at=f"2026-01-{index + 1:02d}T06:00:00Z",
                    value=value,
                )
            )
        result = registry.call(
            "valkit.get_drift", {"agent_id": "rave-als-generator"}, context
        )
        assert len(result["spc_points"]) == 5
        assert any(v["rule"] == "WE1" for v in result["violations"])

    def test_opens_a_change_control_with_a_derived_scope(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        result = registry.call(
            "valkit.open_change_control",
            {
                "agent_id": "rave-als-generator",
                "reason": "Model version bumped.",
                "trigger": "model_version",
            },
            context,
        )
        assert result["cc_id"]
        assert "judge_calibration" in result["required_scope"]
        assert "field_accuracy" in result["required_scope"]

    def test_a_drift_trigger_scopes_narrowly(self, registry, context):
        registry.call("valkit.ingest_spec", {"yaml": SPEC_YAML}, context)
        result = registry.call(
            "valkit.open_change_control",
            {
                "agent_id": "rave-als-generator",
                "reason": "field_accuracy dropped below its limit.",
                "trigger": "drift",
                "metrics": ["field_accuracy"],
            },
            context,
        )
        assert "citation_accuracy" not in result["required_scope"]

    def test_an_unconfigured_collaborator_is_reported_clearly(self, registry):
        bare = ValKitToolContext()
        result = registry.call("valkit.get_drift", {"agent_id": "a"}, bare)
        assert "no drift monitor is configured" in result["error"]


class TestServer:
    def test_the_module_imports_without_the_mcp_package(self):
        """Referencing the server must never require the optional extra."""
        import valkit.mcp

        assert callable(valkit.mcp.build_registry)

    def test_building_a_server_without_mcp_gives_a_clear_error(self, monkeypatch, context):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("mcp"):
                raise ImportError("No module named 'mcp'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        from valkit.mcp.server import build_server

        with pytest.raises(ValKitError, match=r"pip install 'valkit\[mcp\]'"):
            build_server(context)

    def test_every_registry_tool_would_be_exposed(self, registry):
        """The registry is the source of truth; the server adds nothing."""
        from valkit.mcp import server

        exposed = []

        class FakeServer:
            def add_tool(self, handler, name, description):
                exposed.append(name)

        fake = FakeServer()
        for definition in registry.definitions():
            server._register(fake, registry, definition, ValKitToolContext())
        assert exposed == registry.names()

    def test_bound_handlers_do_not_capture_the_loop_variable(self, registry, context):
        """Each handler must dispatch to its own tool, not the last one."""
        from valkit.mcp import server

        handlers = {}

        class FakeServer:
            def add_tool(self, handler, name, description):
                handlers[name] = handler

        fake = FakeServer()
        for definition in registry.definitions():
            server._register(fake, registry, definition, context)

        result = json.loads(handlers["valkit.ingest_spec"](yaml=SPEC_YAML))
        assert result["agent_id"] == "rave-als-generator"
