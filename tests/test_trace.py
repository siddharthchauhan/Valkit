"""Tests for the traceability graph and the RTM.

The graph's job is to find gaps, so most of these tests introduce a gap and
check that it is reported with the right severity. A validation tool that
reports full coverage for an incomplete package is worse than no tool.
"""

from __future__ import annotations

import pytest

from valkit.models import (
    Document,
    DocumentStatus,
    DocumentType,
    QualificationPhase,
    Requirement,
    RequirementKind,
    Risk,
    TestCase,
    TestExecution,
)
from valkit.spec import derive_all, load_spec
from valkit.trace.graph import TraceabilityGraph, TraceNode
from valkit.trace.rtm import build_rtm, natural_key, render_compact, render_csv, render_markdown


@pytest.fixture
def bundle():
    return derive_all(load_spec("examples/valkit.yaml"))


@pytest.fixture
def graph(bundle):
    return TraceabilityGraph.from_records(
        requirements=bundle.requirements, risks=bundle.risks, tests=bundle.tests
    )


@pytest.fixture
def executed_graph(bundle):
    executions = [
        TestExecution(
            test_id=test.test_id,
            run_id="RUN-0001",
            executed_at="2026-01-01T00:00:00Z",
            passed=True,
            evidence_refs=["a" * 64],
        )
        for test in bundle.tests
    ]
    return TraceabilityGraph.from_records(
        requirements=bundle.requirements,
        risks=bundle.risks,
        tests=bundle.tests,
        executions=executions,
    )


class TestGraphConstruction:
    def test_nodes_are_created_for_every_record_type(self, graph):
        assert graph.nodes_of("requirement")
        assert graph.nodes_of("risk")
        assert graph.nodes_of("test")

    def test_edges_come_from_the_records(self, graph, bundle):
        test = bundle.tests[0]
        assert set(graph.targets_of("test", test.test_id, "verifies")) == set(
            test.requirement_ids
        )

    def test_parent_requirements_are_linked(self, graph, bundle):
        functional = [r for r in bundle.requirements if r.parent_ids][0]
        assert graph.targets_of("requirement", functional.req_id, "implements") == (
            functional.parent_ids
        )

    def test_signatures_are_linked_to_their_document(self):
        from valkit.models import Signature, SignatureMeaning

        signature = Signature(
            signature_id="SIG-1",
            document_id="DOC-1",
            document_sha256="a" * 64,
            signer_id="qa",
            printed_name="Q A",
            meaning=SignatureMeaning.APPROVED,
            signed_at="2026-01-01T00:00:00Z",
        )
        document = Document(
            doc_id="DOC-1",
            doc_type=DocumentType.VSR,
            title="VSR",
            agent_id="a",
            agent_version="1",
            content="x",
            content_sha256="b" * 64,
            generated_at="2026-01-01T00:00:00Z",
            signatures=[signature],
        )
        graph = TraceabilityGraph.from_records(documents=[document])
        assert graph.targets_of("signature", "SIG-1", "signs") == ["DOC-1"]

    def test_empty_graph_is_valid(self):
        assert TraceabilityGraph().validate().ok


class TestValidation:
    def test_derived_package_has_full_critical_coverage(self, graph):
        findings = graph.validate().of_kind("uncovered_requirement")
        assert not [f for f in findings if f.blocking]

    def test_an_uncovered_critical_requirement_is_blocking(self, bundle):
        # Drop the OQ that uniquely verifies the field_accuracy requirement.
        # Most requirements have several verifiers, so removing an arbitrary
        # test would not orphan anything.
        remaining = [t for t in bundle.tests if t.metric_name != "field_accuracy"]
        graph = TraceabilityGraph.from_records(
            requirements=bundle.requirements, risks=bundle.risks, tests=remaining
        )
        findings = graph.validate().of_kind("uncovered_requirement")
        assert any(f.blocking for f in findings)

    def test_removing_a_test_is_reported_not_silently_absorbed(self, bundle):
        """The check must be able to fail, or it proves nothing."""
        without_oq = [t for t in bundle.tests if t.metric_name != "field_accuracy"]
        graph = TraceabilityGraph.from_records(
            requirements=bundle.requirements, risks=bundle.risks, tests=without_oq
        )
        validation = graph.validate()
        assert not validation.ok
        uncovered = validation.of_kind("uncovered_requirement")
        assert uncovered and uncovered[0].node_ids

    def test_a_non_critical_gap_is_advisory_not_blocking(self):
        requirement = Requirement(
            req_id="URS-99",
            kind=RequirementKind.USER,
            text="A nice to have.",
            critical=False,
        )
        graph = TraceabilityGraph.from_records(requirements=[requirement])
        findings = graph.validate().of_kind("uncovered_requirement")
        assert findings and not findings[0].blocking

    def test_dangling_reference_is_blocking(self):
        test = TestCase(
            test_id="OQ-01",
            phase=QualificationPhase.OQ,
            title="t",
            objective="o",
            acceptance_text="a",
            requirement_ids=["URS-DOES-NOT-EXIST"],
        )
        validation = TraceabilityGraph.from_records(tests=[test]).validate()
        assert not validation.ok
        assert validation.of_kind("dangling_reference")

    def test_unmitigated_risk_is_blocking(self):
        requirement = Requirement(req_id="URS-01", kind=RequirementKind.USER, text="r")
        risk = Risk(risk_id="RISK-01", description="d", failure_mode="f", requirement_ids=["URS-01"])
        test = TestCase(
            test_id="OQ-01",
            phase=QualificationPhase.OQ,
            title="t",
            objective="o",
            acceptance_text="a",
            requirement_ids=["URS-01"],
        )
        validation = TraceabilityGraph.from_records(
            requirements=[requirement], risks=[risk], tests=[test]
        ).validate()
        assert validation.of_kind("unmitigated_risk")

    def test_unexecuted_tests_are_blocking(self, graph):
        assert any(f.blocking for f in graph.validate().of_kind("test_not_executed"))

    def test_executed_package_has_no_execution_finding(self, executed_graph):
        assert not executed_graph.validate().of_kind("test_not_executed")

    def test_execution_without_evidence_is_blocking(self, bundle):
        executions = [
            TestExecution(
                test_id=test.test_id,
                run_id="RUN-1",
                executed_at="2026-01-01T00:00:00Z",
                passed=True,
                evidence_refs=[],
            )
            for test in bundle.tests
        ]
        graph = TraceabilityGraph.from_records(
            requirements=bundle.requirements,
            risks=bundle.risks,
            tests=bundle.tests,
            executions=executions,
        )
        findings = graph.validate().of_kind("execution_without_evidence")
        assert findings and findings[0].blocking
        assert "assertion, not a demonstration" in findings[0].message

    def test_approved_document_without_a_signature_is_blocking(self):
        document = Document(
            doc_id="DOC-1",
            doc_type=DocumentType.VSR,
            title="VSR",
            agent_id="a",
            agent_version="1",
            content="x",
            content_sha256="b" * 64,
            generated_at="2026-01-01T00:00:00Z",
            status=DocumentStatus.APPROVED,
        )
        validation = TraceabilityGraph.from_records(documents=[document]).validate()
        assert validation.of_kind("approved_without_signature")

    def test_a_cycle_is_detected(self):
        graph = TraceabilityGraph()
        for node_id in ("A", "B", "C"):
            graph.add_node(TraceNode("requirement", node_id))
        graph.link("requirement", "A", "implements", "requirement", "B")
        graph.link("requirement", "B", "implements", "requirement", "C")
        graph.link("requirement", "C", "implements", "requirement", "A")
        findings = graph.validate().of_kind("cycle")
        assert findings

    def test_an_acyclic_graph_reports_no_cycle(self, graph):
        assert not graph.validate().of_kind("cycle")


class TestCoverage:
    def test_full_critical_coverage_on_the_derived_package(self, graph):
        coverage = graph.coverage()
        assert coverage.critical_covered == coverage.critical_total
        assert coverage.complete

    def test_coverage_is_not_complete_with_a_critical_gap(self, bundle):
        remaining = [t for t in bundle.tests if t.metric_name != "field_accuracy"]
        graph = TraceabilityGraph.from_records(
            requirements=bundle.requirements, risks=bundle.risks, tests=remaining
        )
        assert not graph.coverage().complete

    def test_execution_rate_reflects_executions(self, graph, executed_graph):
        assert graph.coverage().execution_rate == 0.0
        assert executed_graph.coverage().execution_rate == 1.0

    def test_ratios_are_exact_fractions(self, executed_graph):
        coverage = executed_graph.coverage()
        assert coverage.critical_coverage == (
            coverage.critical_covered / coverage.critical_total
        )


class TestRendering:
    def test_mermaid_is_produced(self, graph):
        diagram = graph.render_mermaid(max_nodes=20)
        assert diagram.startswith("graph LR")
        assert "-->" in diagram

    def test_mermaid_respects_the_node_cap(self, graph):
        diagram = graph.render_mermaid(max_nodes=5)
        assert "showing 5 of" in diagram

    def test_mermaid_focus_stays_near_the_focused_node(self, graph, bundle):
        focused = graph.render_mermaid(
            focus=("requirement", bundle.requirements[0].req_id), max_nodes=8
        )
        assert bundle.requirements[0].req_id.replace("-", "_") in focused

    def test_mermaid_escapes_quotes_in_labels(self):
        graph = TraceabilityGraph()
        graph.add_node(TraceNode("requirement", 'URS-1"odd', 'a "quoted" label'))
        line = graph.render_mermaid().split("\n")[1]
        identifier = line.strip().split("[", 1)[0]
        assert '"' not in identifier
        assert identifier.replace("_", "").isalnum()

    def test_dot_is_produced(self, graph):
        dot = graph.to_dot()
        assert dot.startswith("digraph traceability {")
        assert dot.rstrip().endswith("}")


class TestRtm:
    def test_rows_are_naturally_sorted(self, executed_graph):
        rows = build_rtm(executed_graph)
        ids = [row.requirement_id for row in rows]
        assert ids == sorted(ids, key=natural_key)

    def test_natural_sort_places_2_before_10(self):
        assert sorted(["URS-10", "URS-2"], key=natural_key) == ["URS-2", "URS-10"]

    def test_verified_when_executed_and_passed(self, executed_graph):
        rows = build_rtm(executed_graph)
        assert all(row.verdict == "verified" for row in rows)

    def test_not_executed_when_tests_never_ran(self, graph):
        rows = build_rtm(graph)
        assert all(row.verdict == "not executed" for row in rows)

    def test_not_verified_when_an_execution_failed(self, bundle):
        executions = [
            TestExecution(
                test_id=test.test_id,
                run_id="RUN-1",
                executed_at="2026-01-01T00:00:00Z",
                passed=(test.test_id != "OQ-01"),
                evidence_refs=["a" * 64],
            )
            for test in bundle.tests
        ]
        rows = build_rtm(
            TraceabilityGraph.from_records(
                requirements=bundle.requirements,
                risks=bundle.risks,
                tests=bundle.tests,
                executions=executions,
            )
        )
        assert any(row.verdict == "not verified" for row in rows)

    def test_no_test_verdict(self):
        requirement = Requirement(req_id="URS-01", kind=RequirementKind.USER, text="orphan")
        rows = build_rtm(requirements=[requirement])
        assert rows[0].verdict == "no test"

    def test_markdown_always_shows_the_gap_section(self, executed_graph):
        """The heading must be present even when there is nothing under it."""
        markdown = render_markdown(build_rtm(executed_graph), executed_graph.coverage())
        assert "### Requirements with no verifying test" in markdown
        assert "None. Every requirement is linked to at least one test." in markdown

    def test_markdown_names_critical_gaps_prominently(self):
        requirement = Requirement(
            req_id="URS-01", kind=RequirementKind.USER, text="orphan", critical=True
        )
        markdown = render_markdown(build_rtm(requirements=[requirement]))
        assert "**CRITICAL**" in markdown
        assert "is not validated" in markdown

    def test_markdown_does_not_report_nothing_as_complete(self, graph):
        """0 of 0 executions must not render as 100%."""
        markdown = render_markdown(build_rtm(graph), graph.coverage())
        assert "0 (not applicable)" in markdown
        assert "0 of 0 (100%)" not in markdown

    def test_csv_export_has_a_header_and_a_row_per_requirement(self, executed_graph):
        rows = build_rtm(executed_graph)
        text = render_csv(rows)
        lines = text.strip().split("\n")
        assert lines[0].startswith("requirement_id,")
        assert len(lines) == len(rows) + 1

    def test_compact_chain_form(self, executed_graph):
        compact = render_compact(build_rtm(executed_graph))
        assert " -> " in compact
        assert "[verified]" in compact

    def test_evidence_digests_are_abbreviated(self, executed_graph):
        markdown = render_markdown(build_rtm(executed_graph), executed_graph.coverage())
        assert "a" * 64 not in markdown
        assert "…" in markdown
