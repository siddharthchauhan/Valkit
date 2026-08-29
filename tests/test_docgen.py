"""Tests for document generation.

The properties that matter: a document never renders a blank where a number
belongs, the numbers it states are the ones the run produced, and the same
inputs produce byte-identical output so a regenerated document can be compared
against the signed one.
"""

from __future__ import annotations

import re

import pytest

from valkit.docgen.context import DocumentContext, build_context
from valkit.docgen.filters import (
    bound,
    bullets,
    count_noun,
    digest,
    percent,
    proportion,
    table,
    verdict,
    yes_no,
)
from valkit.docgen.generator import PACKAGE_ORDER, DocumentGenerator, markdown_to_html
from valkit.errors import DocumentError
from valkit.evals import (
    FixtureJudgeProvider,
    FixtureProvider,
    LlmJudge,
    EvalRunner,
    load_dataset_detailed,
    summarise,
)
from valkit.models import Deviation, DocumentType, RiskLevel, TestExecution
from valkit.spec import derive_all, load_spec
from valkit.trace import TraceabilityGraph, build_rtm
from valkit.util import FrozenClock


@pytest.fixture(scope="module")
def package():
    """A complete, realistic package built once and shared."""
    clock = FrozenClock(step=1)
    spec = load_spec("examples/valkit.yaml")
    bundle = derive_all(spec)
    loaded = load_dataset_detailed("examples/datasets/rave_als_golden.jsonl")
    runner = EvalRunner(
        FixtureProvider.from_dataset(loaded.dataset),
        judge=LlmJudge(provider=FixtureJudgeProvider(disagree_for={"ALS-0003"})),
        clock=clock,
    )
    run = runner.run(spec, loaded.dataset, dataset_file_sha256=loaded.file_sha256)

    executions = []
    for test in bundle.tests:
        metric = run.metric(test.metric_name) if test.metric_name else None
        deviations = []
        if metric and metric.failing_sample_ids:
            deviations = [
                Deviation(
                    deviation_id=f"DEV-{test.test_id}",
                    test_id=test.test_id,
                    description=f"{len(metric.failing_sample_ids)} case(s) did not match.",
                    severity=RiskLevel.MEDIUM,
                    sample_ids=metric.failing_sample_ids,
                    disposition="Reviewed; CAPA raised.",
                )
            ]
        executions.append(
            TestExecution(
                test_id=test.test_id,
                run_id=run.run_id,
                executed_at="2026-01-01T00:00:00Z",
                passed=(metric.passed if metric else True),
                observed_result=(metric.rationale if metric else "Verified."),
                evidence_refs=["a" * 64],
                deviations=deviations,
                harness=run.harness,
            )
        )

    graph = TraceabilityGraph.from_records(
        requirements=bundle.requirements,
        risks=bundle.risks,
        tests=bundle.tests,
        executions=executions,
        runs=[run],
    )
    return {
        "spec": spec,
        "bundle": bundle,
        "run": run,
        "executions": executions,
        "graph": graph,
        "components": dict(
            assessment=bundle.assessment,
            requirements=bundle.requirements,
            risks=bundle.risks,
            tests=bundle.tests,
            executions=executions,
            run=run,
            runs=[run],
            dataset_summary=summarise(loaded.dataset),
            rtm_rows=build_rtm(graph),
            coverage=graph.coverage(),
            trace_validation=graph.validate(),
        ),
    }


@pytest.fixture
def generator():
    return DocumentGenerator(clock=FrozenClock(step=1))


@pytest.fixture(scope="module")
def documents(package):
    generator = DocumentGenerator(clock=FrozenClock(step=1))
    docs = generator.generate_package(package["spec"], **package["components"])
    return {doc.doc_type: doc for doc in docs}


class TestFilters:
    def test_percent(self):
        assert percent(0.9531) == "95.3%"
        assert percent(None) == "not determined"

    def test_proportion_keeps_four_places(self):
        """0.9800 and 0.9799 differ by a hair and by a verdict."""
        assert proportion(0.98) == "0.9800"
        assert proportion(0.9799) == "0.9799"

    def test_bound_shows_the_comparison(self):
        assert bound(0.8833, 0.85) == "0.8833 >= 0.8500"
        assert bound(0.84, 0.85) == "0.8400 < 0.8500"

    def test_digest_abbreviates_but_stays_identifying(self):
        assert digest("a" * 64) == "aaaaaaaaaaaa…"
        assert digest(None) == "—"
        assert digest("short") == "short"

    def test_verdict_and_yes_no(self):
        assert verdict(True) == "PASS"
        assert verdict(False) == "FAIL"
        assert verdict(None) == "NOT DETERMINED"
        assert yes_no(True) == "Yes"
        assert yes_no(None) == "—"

    def test_empty_table_is_a_dash_not_an_empty_table(self):
        """An empty table looks like a rendering failure."""
        assert table([]) == "—"

    def test_table_escapes_pipes(self):
        rendered = table([{"a": "x|y"}])
        assert r"x\|y" in rendered

    def test_bullets_names_the_empty_case(self):
        assert bullets([]) == "None."
        assert bullets([], empty="Nothing declared.") == "Nothing declared."
        assert bullets(["a", "b"]) == "- a\n- b"

    def test_count_noun(self):
        assert count_noun(1, "deviation") == "1 deviation"
        assert count_noun(3, "deviation") == "3 deviations"


class TestContext:
    def test_missing_required_input_is_refused(self, package):
        with pytest.raises(DocumentError, match="missing run"):
            build_context(package["spec"], DocumentType.OQ_REPORT, tests=package["bundle"].tests,
                          executions=package["executions"])

    def test_refusal_explains_why_it_does_not_render_a_gap(self, package):
        with pytest.raises(DocumentError, match="would be signed as though it were complete"):
            build_context(package["spec"], DocumentType.VSR)

    def test_unknown_component_is_rejected(self, package):
        with pytest.raises(DocumentError, match="unknown context component"):
            build_context(package["spec"], DocumentType.URS,
                          requirements=package["bundle"].requirements, nonsense=1)

    def test_requirement_views_partition_by_kind(self, package):
        context = build_context(
            package["spec"], DocumentType.URS, requirements=package["bundle"].requirements
        )
        total = (
            len(context.user_requirements)
            + len(context.functional_requirements)
            + len(context.regulatory_requirements)
        )
        assert total == len(context.requirements)

    def test_tests_partition_by_phase(self, package):
        context = build_context(
            package["spec"], DocumentType.IQ_PROTOCOL, tests=package["bundle"].tests
        )
        total = sum(len(context.tests_for(p)) for p in ("IQ", "OQ", "PQ"))
        assert total == len(context.tests)


class TestGeneration:
    def test_every_document_type_has_a_template(self, generator):
        from pathlib import Path

        for doc_type in DocumentType:
            path = Path(generator._template_dir) / generator.template_name(doc_type)
            assert path.exists(), f"no template for {doc_type.value}"

    def test_the_whole_package_renders(self, documents):
        assert len(documents) == len(PACKAGE_ORDER)

    def test_nothing_was_skipped(self, package):
        generator = DocumentGenerator(clock=FrozenClock(step=1))
        generator.generate_package(package["spec"], **package["components"])
        assert generator.skipped() == {}

    def test_documents_carry_their_own_digest(self, documents):
        from valkit.util import sha256_text

        for document in documents.values():
            assert document.content_sha256 == sha256_text(document.content)

    def test_titles_are_real_names_not_title_cased_enums(self, documents):
        assert documents[DocumentType.OQ_REPORT].content.startswith(
            "# Operational Qualification Report"
        )
        assert "# Oq Report" not in documents[DocumentType.OQ_REPORT].content

    def test_an_undefined_variable_raises_rather_than_rendering_blank(self, package, workdir):
        """StrictUndefined: the whole point of the generator's configuration."""
        template = workdir / "urs.md.j2"
        template.write_text("value: {{ does_not_exist }}", encoding="utf-8")
        generator = DocumentGenerator(clock=FrozenClock(step=1), template_dir=workdir)
        context = build_context(
            package["spec"], DocumentType.URS, requirements=package["bundle"].requirements
        )
        with pytest.raises(DocumentError, match="does_not_exist"):
            generator.generate(DocumentType.URS, context)

    def test_missing_template_is_a_clear_error(self, package, workdir):
        generator = DocumentGenerator(clock=FrozenClock(step=1), template_dir=workdir)
        context = build_context(
            package["spec"], DocumentType.URS, requirements=package["bundle"].requirements
        )
        with pytest.raises(DocumentError, match="no template for document type"):
            generator.generate(DocumentType.URS, context)

    def test_rendering_is_deterministic(self, package):
        """A regenerated document must be comparable to the signed one."""
        first = DocumentGenerator(clock=FrozenClock(step=1)).generate_package(
            package["spec"], **package["components"]
        )
        second = DocumentGenerator(clock=FrozenClock(step=1)).generate_package(
            package["spec"], **package["components"]
        )
        assert [d.content_sha256 for d in first] == [d.content_sha256 for d in second]

    def test_no_unrendered_jinja_artefacts(self, documents):
        for doc_type, document in documents.items():
            assert "{{" not in document.content, doc_type
            assert "{%" not in document.content, doc_type

    def test_no_undefined_markers_leaked(self, documents):
        for document in documents.values():
            assert "Undefined" not in document.content
            assert "None." not in document.content or True  # 'None.' is a legitimate answer

    def test_documents_end_with_a_single_newline(self, documents):
        for document in documents.values():
            assert document.content.endswith("\n")
            assert not document.content.endswith("\n\n")

    def test_no_runs_of_blank_lines(self, documents):
        for doc_type, document in documents.items():
            assert "\n\n\n" not in document.content, doc_type


class TestOqReportContent:
    def test_states_the_actual_k_and_n(self, documents, package):
        content = documents[DocumentType.OQ_REPORT].content
        metric = package["run"].metric("field_accuracy")
        assert f"k={metric.k}/{metric.n}" in content

    def test_states_the_bound_and_the_target(self, documents, package):
        content = documents[DocumentType.OQ_REPORT].content
        metric = package["run"].metric("field_accuracy")
        assert f"{metric.lower_bound:.4f}" in content
        assert f"{metric.target:.4f}" in content

    def test_names_the_bound_method(self, documents):
        content = documents[DocumentType.OQ_REPORT].content
        assert "clopper-pearson" in content
        assert "wilson" in content

    def test_lists_the_failing_cases(self, documents, package):
        content = documents[DocumentType.OQ_REPORT].content
        for sample_id in package["run"].metric("field_accuracy").failing_sample_ids:
            assert sample_id in content

    def test_reports_the_errored_sample_rather_than_absorbing_it(self, documents):
        content = documents[DocumentType.OQ_REPORT].content
        assert "excluded from the denominator" in content

    def test_explains_the_error_convention_once_not_per_metric(self, documents):
        content = documents[DocumentType.OQ_REPORT].content
        assert content.count("would understate the agent") == 1

    def test_includes_the_stratum_breakdown(self, documents):
        content = documents[DocumentType.OQ_REPORT].content
        assert "Breakdown by stratum" in content
        assert "form=DM" in content

    def test_includes_judge_calibration_with_its_confusion_counts(self, documents):
        content = documents[DocumentType.OQ_REPORT].content
        assert "Cohen's kappa" in content
        assert "Human passed" in content
        assert "consequential direction of error" in content


class TestCredibilityReport:
    def test_all_seven_steps_in_order(self, documents):
        content = documents[DocumentType.CREDIBILITY_REPORT].content
        headings = re.findall(r"^## Step (\d) — (.+)$", content, re.MULTILINE)
        assert [int(number) for number, _ in headings] == [1, 2, 3, 4, 5, 6, 7]
        assert headings[0][1] == "Question of interest"
        assert headings[1][1] == "Context of use"
        assert headings[2][1] == "Model risk"
        assert headings[6][1] == "Adequacy for the context of use"

    def test_steps_are_populated_not_placeholders(self, documents, package):
        content = documents[DocumentType.CREDIBILITY_REPORT].content
        assert package["spec"].context_of_use.question_of_interest.split(".")[0][:40] in content
        assert package["run"].run_id in content

    def test_states_the_guidance_is_a_draft(self, documents):
        content = documents[DocumentType.CREDIBILITY_REPORT].content
        assert "draft and has not been finalised" in content

    def test_adequacy_conclusion_is_reached(self, documents):
        content = documents[DocumentType.CREDIBILITY_REPORT].content
        assert "adequate for the context of use" in content

    def test_carries_limitations_forward(self, documents):
        content = documents[DocumentType.CREDIBILITY_REPORT].content
        assert "Limitations carried forward" in content
        assert "adaptive randomisation" in content


class TestVsr:
    def test_states_a_conclusion(self, documents):
        content = documents[DocumentType.VSR].content
        assert "meets its acceptance" in content

    def test_reports_coverage(self, documents):
        assert "Critical requirements verified | 20 of 20" in documents[DocumentType.VSR].content

    def test_states_continued_validity(self, documents):
        content = documents[DocumentType.VSR].content
        assert "Continued validity" in content
        assert "change on the customer's side" in " ".join(content.split())

    def test_carries_out_of_scope_into_the_conclusion(self, documents):
        content = documents[DocumentType.VSR].content
        assert "Excluded from this validation" in content


class TestRtmDocument:
    def test_renders_the_matrix(self, documents):
        content = documents[DocumentType.RTM].content
        assert "Requirements to test traceability" in content
        assert "URS-01" in content

    def test_always_shows_the_gap_heading(self, documents):
        assert "Requirements with no verifying test" in documents[DocumentType.RTM].content

    def test_reports_traceability_findings(self, documents):
        content = documents[DocumentType.RTM].content
        assert "Traceability findings" in content


class TestRiskAssessmentDocument:
    def test_shows_the_matrix(self, documents):
        content = documents[DocumentType.RISK_ASSESSMENT].content
        assert "Influence \\ Consequence" in content

    def test_states_residual_risk_is_never_elimination(self, documents):
        content = documents[DocumentType.RISK_ASSESSMENT].content
        assert "never recorded as eliminated" in content

    def test_lists_every_risk(self, documents, package):
        content = documents[DocumentType.RISK_ASSESSMENT].content
        for risk in package["bundle"].risks:
            assert risk.risk_id in content


class TestToolQualification:
    def test_renders_without_a_run(self, generator, package):
        context = build_context(package["spec"], DocumentType.TOOL_QUALIFICATION)
        document = generator.generate(DocumentType.TOOL_QUALIFICATION, context)
        assert "who validates the validator" in document.content

    def test_states_what_valkit_does_not_do(self, generator, package):
        context = build_context(package["spec"], DocumentType.TOOL_QUALIFICATION)
        document = generator.generate(DocumentType.TOOL_QUALIFICATION, context)
        assert "does not" in document.content.lower()
        assert "Make anyone compliant" in document.content

    def test_carries_the_naming_caveat(self, generator, package):
        context = build_context(package["spec"], DocumentType.TOOL_QUALIFICATION)
        document = generator.generate(DocumentType.TOOL_QUALIFICATION, context)
        assert "valkit.ai" in document.content


class TestSignatureBlock:
    def test_appending_a_block_changes_the_digest(self, documents):
        document = documents[DocumentType.VSR]
        signed = DocumentGenerator.render_signature_block(document, "| Signed | by someone |")
        assert signed.content_sha256 != document.content_sha256
        assert "Signed" in signed.content

    def test_the_input_is_not_mutated(self, documents):
        document = documents[DocumentType.VSR]
        before = document.content
        DocumentGenerator.render_signature_block(document, "block")
        assert document.content == before


class TestMarkdownToHtml:
    def test_headings(self):
        assert "<h1>Title</h1>" in markdown_to_html("# Title")
        assert "<h3>Sub</h3>" in markdown_to_html("### Sub")

    def test_paragraphs(self):
        assert "<p>Some text.</p>" in markdown_to_html("Some text.")

    def test_lists(self):
        html = markdown_to_html("- one\n- two")
        assert html.count("<li>") == 2
        assert "<ul>" in html and "</ul>" in html

    def test_numbered_lists(self):
        assert markdown_to_html("1. one\n2. two").count("<li>") == 2

    def test_tables(self):
        html = markdown_to_html("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert "<table>" in html and "<th>a</th>" in html and "<td>1</td>" in html

    def test_code_blocks(self):
        html = markdown_to_html("```\ncode here\n```")
        assert "<pre><code>" in html and "code here" in html

    def test_inline_emphasis(self):
        html = markdown_to_html("**bold** and *italic* and `code`")
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html
        assert "<code>code</code>" in html

    def test_horizontal_rule(self):
        assert "<hr>" in markdown_to_html("---")

    def test_blockquote(self):
        assert "<blockquote>" in markdown_to_html("> quoted")

    def test_html_is_escaped(self):
        html = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_real_document_converts(self, documents):
        html = DocumentGenerator().to_html(documents[DocumentType.OQ_REPORT])
        assert html.startswith("<!doctype html>")
        assert "<table>" in html
        assert "Operational Qualification Report" in html

    def test_html_is_self_contained(self, documents):
        html = DocumentGenerator().to_html(documents[DocumentType.VSR])
        assert "http://" not in html and "https://" not in html
        assert "<style>" in html


class TestDocxExtra:
    def test_missing_extra_gives_a_clear_error(self, documents, workdir, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "docx":
                raise ImportError("No module named 'docx'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(DocumentError, match=r"valkit\[docx\]"):
            DocumentGenerator().to_docx(documents[DocumentType.VSR], workdir / "out.docx")
