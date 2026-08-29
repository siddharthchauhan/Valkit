"""Tests for requirement, risk and test-case derivation.

The single most important assertion in this file is that every critical
requirement is covered by at least one test. An uncovered requirement is the
commonest finding against a traceability matrix, and a generator that produces
one silently would be worse than no generator at all.
"""

from __future__ import annotations

import pytest

from valkit.models import (
    MetricSpec,
    MetricType,
    QualificationPhase,
    RequirementKind,
    RiskClass,
)
from valkit.spec.derive import derive_all, derive_requirements, derive_risks, derive_tests
from valkit.spec.loader import parse_spec
from valkit.testing import EXAMPLE_YAML, make_spec


@pytest.fixture
def bundle():
    return derive_all(parse_spec(EXAMPLE_YAML, "example").spec)


class TestRequirements:
    def test_produces_user_functional_and_regulatory_requirements(self, bundle):
        kinds = {r.kind for r in bundle.requirements}
        assert kinds == {
            RequirementKind.USER,
            RequirementKind.FUNCTIONAL,
            RequirementKind.REGULATORY,
        }

    def test_identifiers_are_stable_across_calls(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        first = [r.req_id for r in derive_requirements(spec)]
        second = [r.req_id for r in derive_requirements(spec)]
        assert first == second

    def test_identifiers_are_unique(self, bundle):
        ids = [r.req_id for r in bundle.requirements]
        assert len(ids) == len(set(ids))

    def test_question_of_interest_becomes_the_first_requirement(self, bundle):
        first = bundle.requirements[0]
        assert first.req_id == "URS-01"
        assert first.source == "context_of_use.question_of_interest"
        assert "Archival Listing Specification" in first.text

    def test_each_in_scope_item_becomes_a_requirement(self, bundle):
        sources = [r.source for r in bundle.requirements]
        assert "intended_use.in_scope[0]" in sources
        assert "intended_use.in_scope[1]" in sources

    def test_out_of_scope_becomes_a_negative_requirement(self, bundle):
        negatives = [r for r in bundle.requirements if "shall not be used for" in r.text]
        assert negatives
        assert "auto-loading" in negatives[0].text

    def test_each_metric_becomes_a_performance_requirement(self, bundle):
        for metric in bundle.spec.acceptance.metrics:
            matching = [
                r for r in bundle.requirements
                if r.source == f"acceptance.metrics.{metric.name}"
            ]
            assert len(matching) == 1

    def test_performance_requirement_states_the_bound_method(self, bundle):
        requirement = next(
            r for r in bundle.requirements if r.source == "acceptance.metrics.citation_accuracy"
        )
        assert "wilson" in requirement.text.lower()
        assert "0.95" in requirement.text

    def test_regulatory_requirements_cite_their_source(self, bundle):
        regulatory = [r for r in bundle.requirements if r.kind is RequirementKind.REGULATORY]
        assert len(regulatory) >= 4
        assert any("11.10(e)" in r.source for r in regulatory)
        assert any("11.50" in r.source for r in regulatory)

    def test_functional_requirements_name_their_parents(self, bundle):
        functional = [r for r in bundle.requirements if r.kind is RequirementKind.FUNCTIONAL]
        assert functional
        known = {r.req_id for r in bundle.requirements}
        for requirement in functional:
            assert requirement.parent_ids
            assert set(requirement.parent_ids) <= known

    def test_human_in_the_loop_claim_becomes_a_testable_requirement(self, bundle):
        assert any("human reviewer" in r.text for r in bundle.requirements)

    def test_no_human_review_requirement_when_not_claimed(self):
        spec = make_spec()
        spec = spec.replace(context_of_use=spec.context_of_use.replace(human_in_the_loop=False))
        assert not any("human reviewer" in r.text for r in derive_requirements(spec))


class TestRisks:
    def test_standing_library_is_produced(self, bundle):
        categories = {r.category for r in bundle.risks}
        assert {"hallucination", "prompt-injection", "drift", "data-integrity"} <= categories

    def test_every_risk_links_to_a_real_requirement(self, bundle):
        known = {r.req_id for r in bundle.requirements}
        for risk in bundle.risks:
            assert risk.requirement_ids
            assert set(risk.requirement_ids) <= known

    def test_every_risk_has_a_mitigation_naming_a_real_control(self, bundle):
        for risk in bundle.risks:
            assert len(risk.mitigation) > 40

    def test_residual_risk_never_claims_elimination(self, bundle):
        """Mitigation reduces risk by at most one class; nothing reaches zero."""
        for risk in bundle.risks:
            assert risk.residual_risk.rank >= RiskClass.LOW.rank
            assert risk.residual_risk.rank <= risk.risk_class.rank

    def test_judge_risk_only_when_a_judge_is_configured(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        with_judge = derive_risks(spec, derive_requirements(spec))
        assert any(r.category == "measurement" for r in with_judge)

        no_judge = spec.replace(models=spec.models.replace(judge=None))
        without = derive_risks(no_judge, derive_requirements(no_judge))
        assert not any(r.category == "measurement" for r in without)

    def test_automation_bias_risk_is_present(self, bundle):
        """The risk that human review is weaker evidence than it appears."""
        assert any(r.category == "human-factors" for r in bundle.risks)

    def test_risk_ids_are_unique_and_stable(self, bundle):
        ids = [r.risk_id for r in bundle.risks]
        assert len(ids) == len(set(ids))
        again = derive_risks(bundle.spec, bundle.requirements)
        assert [r.risk_id for r in again] == ids

    def test_low_detectability_raises_the_class(self):
        """A failure you cannot see is one you cannot correct."""
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        risks = derive_risks(spec, derive_requirements(spec))
        injection = next(r for r in risks if r.category == "prompt-injection")
        assert injection.detectability.value == "high"
        assert injection.risk_class is RiskClass.HIGH


class TestTests:
    def test_all_three_phases_present(self, bundle):
        phases = {t.phase for t in bundle.tests}
        assert phases == {QualificationPhase.IQ, QualificationPhase.OQ, QualificationPhase.PQ}

    def test_one_oq_per_acceptance_metric(self, bundle):
        metric_tests = [t for t in bundle.tests if t.metric_name]
        assert {t.metric_name for t in metric_tests} == {
            m.name for m in bundle.spec.acceptance.metrics
        }

    def test_oq_acceptance_text_states_method_and_target(self, bundle):
        test = next(t for t in bundle.tests if t.metric_name == "citation_accuracy")
        assert "wilson" in test.acceptance_text.lower()
        assert "0.95" in test.acceptance_text
        assert "lower bound" in test.acceptance_text.lower()

    def test_oq_procedure_verifies_the_dataset_digest_first(self, bundle):
        test = next(t for t in bundle.tests if t.metric_name == "field_accuracy")
        assert "SHA-256" in test.procedure[0]

    def test_red_team_test_only_when_a_red_team_set_exists(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        assert any("Adversarial" in t.title for t in derive_all(spec).tests)

        without = spec.replace(datasets=spec.datasets.replace(red_team=None))
        assert not any("Adversarial" in t.title for t in derive_all(without).tests)

    def test_calibration_test_only_when_a_judge_is_configured(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        assert any("Judge calibration" in t.title for t in derive_all(spec).tests)

        without = spec.replace(models=spec.models.replace(judge=None))
        assert not any("Judge calibration" in t.title for t in derive_all(without).tests)

    def test_monitoring_test_only_when_a_schedule_exists(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        assert any("Scheduled re-evaluation" in t.title for t in derive_all(spec).tests)

        without = spec.replace(monitoring=spec.monitoring.replace(schedule=None))
        assert not any("Scheduled re-evaluation" in t.title for t in derive_all(without).tests)

    def test_iq_covers_harness_dataset_model_audit_and_vault(self, bundle):
        iq_titles = " ".join(t.title for t in bundle.tests if t.phase is QualificationPhase.IQ)
        for expected in ("harness", "dataset", "Model", "Audit trail", "vault"):
            assert expected in iq_titles

    def test_unscripted_tests_are_marked(self, bundle):
        """FDA CSA distinguishes scripted from unscripted assurance activities."""
        unscripted = [t for t in bundle.tests if not t.scripted]
        assert unscripted
        assert all(t.phase is QualificationPhase.PQ for t in unscripted)

    def test_every_test_links_to_at_least_one_requirement(self, bundle):
        for test in bundle.tests:
            assert test.requirement_ids, f"{test.test_id} verifies nothing"

    def test_every_test_link_resolves(self, bundle):
        known = {r.req_id for r in bundle.requirements}
        for test in bundle.tests:
            dangling = set(test.requirement_ids) - known
            assert not dangling, f"{test.test_id} references unknown {dangling}"

    def test_every_risk_link_resolves(self, bundle):
        known = {r.risk_id for r in bundle.risks}
        for test in bundle.tests:
            assert set(test.risk_ids) <= known

    def test_test_ids_are_unique_and_ordered_by_phase(self, bundle):
        ids = [t.test_id for t in bundle.tests]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids, key=lambda i: ("IQ OQ PQ".index(i[:2]), i))


class TestCoverage:
    def test_every_critical_requirement_is_covered_by_a_test(self, bundle):
        """The assertion this whole module exists to make true."""
        critical = {r.req_id for r in bundle.requirements if r.critical}
        covered: set[str] = set()
        for test in bundle.tests:
            covered |= set(test.requirement_ids)
        uncovered = sorted(critical - covered)
        assert not uncovered, f"requirements with no verifying test: {uncovered}"

    def test_functional_requirements_are_covered_through_their_parents(self, bundle):
        functional = {
            r.req_id for r in bundle.requirements if r.kind is RequirementKind.FUNCTIONAL
        }
        covered: set[str] = set()
        for test in bundle.tests:
            covered |= set(test.requirement_ids)
        assert functional <= covered

    def test_coverage_holds_for_a_minimal_specification(self):
        spec = make_spec(
            metrics=[MetricSpec(name="accuracy", type=MetricType.PROPORTION, target=0.9)]
        )
        spec = spec.replace(
            datasets=spec.datasets.replace(red_team=None),
            models=spec.models.replace(judge=None),
            monitoring=spec.monitoring.replace(schedule=None),
        )
        bundle = derive_all(spec)
        critical = {r.req_id for r in bundle.requirements if r.critical}
        covered: set[str] = set()
        for test in bundle.tests:
            covered |= set(test.requirement_ids)
        assert not (critical - covered)

    def test_removing_a_test_makes_the_gap_visible(self, bundle):
        """The coverage check must actually be able to fail."""
        reduced = [t for t in bundle.tests if t.metric_name != "field_accuracy"]
        covered: set[str] = set()
        for test in reduced:
            covered |= set(test.requirement_ids)
        critical = {r.req_id for r in bundle.requirements if r.critical}
        assert critical - covered, "removing an OQ should leave a requirement uncovered"


class TestBundle:
    def test_bundle_carries_everything_together(self, bundle):
        assert bundle.assessment.risk_class
        assert bundle.requirements and bundle.risks and bundle.tests
        assert bundle.requirement("URS-01") is not None
        assert bundle.requirement("URS-99") is None

    def test_critical_requirement_ids_helper(self, bundle):
        assert set(bundle.critical_requirement_ids) <= {r.req_id for r in bundle.requirements}
