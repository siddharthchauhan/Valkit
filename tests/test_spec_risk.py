"""Tests for the model risk assessment.

The property that matters most here is directional: nothing in the risk engine
may lower a risk class on its own. Every escalation rule is tested in isolation,
and a specification override that sits below the derived class is tested to be
recorded rather than silently honoured.
"""

from __future__ import annotations

import pytest

from valkit.models import GampCategory, RegulatoryImpact, RiskClass, RiskLevel
from valkit.spec.risk import RISK_MATRIX, assess_risk, matrix_rationale
from valkit.testing import make_spec


def spec_with_context(**changes):
    spec = make_spec(risk_class=None)
    return spec.replace(context_of_use=spec.context_of_use.replace(**changes))


class TestMatrix:
    def test_every_cell_is_defined(self):
        for influence in RiskLevel:
            for consequence in RiskLevel:
                assert isinstance(RISK_MATRIX[influence][consequence], RiskClass)

    def test_every_cell_has_a_documented_rationale(self):
        for influence in RiskLevel:
            for consequence in RiskLevel:
                note = matrix_rationale(influence, consequence)
                assert note and note[0].isupper() and note.endswith(".")

    def test_matrix_is_monotone_in_influence(self):
        for consequence in RiskLevel:
            ranks = [RISK_MATRIX[i][consequence].rank for i in
                     (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)]
            assert ranks == sorted(ranks)

    def test_matrix_is_monotone_in_consequence(self):
        for influence in RiskLevel:
            ranks = [RISK_MATRIX[influence][c].rank for c in
                     (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)]
            assert ranks == sorted(ranks)

    def test_high_influence_and_high_consequence_is_high_risk(self):
        assert RISK_MATRIX[RiskLevel.HIGH][RiskLevel.HIGH] is RiskClass.HIGH

    @pytest.mark.parametrize("influence", list(RiskLevel))
    @pytest.mark.parametrize("consequence", list(RiskLevel))
    def test_assessment_uses_the_matrix_cell(self, influence, consequence):
        spec = spec_with_context(
            model_influence=influence,
            decision_consequence=consequence,
            patient_safety_impact=False,
            human_in_the_loop=True,
            regulatory_impact=RegulatoryImpact.MEDIUM,
            product_quality_impact=False,
        )
        assessment = assess_risk(spec.replace(gamp=spec.gamp.replace(
            category=GampCategory.CONFIGURED)))
        assert assessment.matrix_cell is RISK_MATRIX[influence][consequence]


class TestEscalationRules:
    def test_baseline_profile(self):
        assert assess_risk(spec_with_context()).risk_class is RiskClass.MEDIUM

    def test_patient_safety_forces_high(self):
        assessment = assess_risk(
            spec_with_context(
                model_influence=RiskLevel.LOW,
                decision_consequence=RiskLevel.LOW,
                patient_safety_impact=True,
            )
        )
        assert assessment.risk_class is RiskClass.HIGH
        assert any("patient-safety" in e for e in assessment.escalations)

    def test_no_human_in_the_loop_escalates_one_level(self):
        with_review = assess_risk(spec_with_context(human_in_the_loop=True))
        without = assess_risk(spec_with_context(human_in_the_loop=False))
        assert without.risk_class.rank == with_review.risk_class.rank + 1
        assert any("no compensating control" in e for e in without.escalations)

    def test_high_regulatory_impact_escalates_one_level(self):
        base = assess_risk(spec_with_context(regulatory_impact=RegulatoryImpact.MEDIUM))
        raised = assess_risk(spec_with_context(regulatory_impact=RegulatoryImpact.HIGH))
        assert raised.risk_class.rank == base.risk_class.rank + 1

    def test_gamp_category_5_sets_a_floor_of_medium(self):
        spec = spec_with_context(
            model_influence=RiskLevel.LOW, decision_consequence=RiskLevel.LOW
        )
        assessment = assess_risk(spec)
        assert assessment.matrix_cell is RiskClass.LOW
        assert assessment.risk_class is RiskClass.MEDIUM
        assert any("category 5" in e for e in assessment.escalations)

    def test_category_4_does_not_get_the_bespoke_floor(self):
        spec = spec_with_context(
            model_influence=RiskLevel.LOW, decision_consequence=RiskLevel.LOW
        )
        spec = spec.replace(gamp=spec.gamp.replace(category=GampCategory.CONFIGURED))
        assert assess_risk(spec).risk_class is RiskClass.LOW

    def test_product_quality_impact_raises_low_to_medium(self):
        spec = spec_with_context(
            model_influence=RiskLevel.LOW,
            decision_consequence=RiskLevel.LOW,
            product_quality_impact=True,
        )
        spec = spec.replace(gamp=spec.gamp.replace(category=GampCategory.CONFIGURED))
        assert assess_risk(spec).risk_class is RiskClass.MEDIUM

    def test_escalations_compose(self):
        spec = spec_with_context(
            model_influence=RiskLevel.LOW,
            decision_consequence=RiskLevel.MEDIUM,
            human_in_the_loop=False,
            regulatory_impact=RegulatoryImpact.HIGH,
        )
        assessment = assess_risk(spec)
        assert assessment.risk_class is RiskClass.HIGH
        assert len(assessment.escalations) >= 2

    def test_no_rule_ever_lowers_the_class(self):
        """Swept across every context combination: derived >= matrix cell."""
        for influence in RiskLevel:
            for consequence in RiskLevel:
                for hitl in (True, False):
                    for safety in (True, False):
                        spec = spec_with_context(
                            model_influence=influence,
                            decision_consequence=consequence,
                            human_in_the_loop=hitl,
                            patient_safety_impact=safety,
                        )
                        assessment = assess_risk(spec)
                        assert assessment.derived_class.rank >= assessment.matrix_cell.rank


class TestOverride:
    def test_higher_override_is_applied_quietly(self):
        spec = make_spec(risk_class=RiskClass.HIGH)
        assessment = assess_risk(spec)
        assert assessment.risk_class is RiskClass.HIGH
        assert assessment.derived_class is RiskClass.MEDIUM
        assert assessment.overridden
        assert any("above the derived class" in e for e in assessment.escalations)

    def test_lower_override_is_applied_but_recorded_as_such(self):
        spec = make_spec(risk_class=RiskClass.LOW)
        assessment = assess_risk(spec)
        assert assessment.risk_class is RiskClass.LOW
        assert assessment.derived_class is RiskClass.MEDIUM
        assert any(e.startswith("OVERRIDE:") for e in assessment.escalations)
        assert "must be justified by the quality function" in " ".join(assessment.escalations)

    def test_lower_override_appears_in_the_rationale(self):
        assessment = assess_risk(make_spec(risk_class=RiskClass.LOW))
        assert "overrides the derived class" in assessment.rationale

    def test_matching_override_is_not_flagged(self):
        assessment = assess_risk(make_spec(risk_class=RiskClass.MEDIUM))
        assert not assessment.overridden


class TestRequiredRigor:
    def test_rigor_scales_with_risk(self):
        low = assess_risk(
            make_spec(risk_class=RiskClass.LOW).replace(
                gamp=make_spec().gamp.replace(risk_class=RiskClass.LOW)
            )
        ).required_rigor
        high = assess_risk(make_spec(risk_class=RiskClass.HIGH)).required_rigor
        assert high.minimum_golden_set > low.minimum_golden_set
        assert high.suggested_target > low.suggested_target
        assert high.review_months < low.review_months

    def test_high_risk_requires_calibration_monitoring_and_red_team(self):
        rigor = assess_risk(make_spec(risk_class=RiskClass.HIGH)).required_rigor
        assert rigor.judge_calibration_required
        assert rigor.monitoring_required
        assert rigor.red_team_required
        assert rigor.independent_approver_required

    def test_minimum_golden_sets_match_the_sample_size_table(self):
        from valkit.stats import min_n_zero_failures

        medium = assess_risk(make_spec(risk_class=RiskClass.MEDIUM)).required_rigor
        high = assess_risk(make_spec(risk_class=RiskClass.HIGH)).required_rigor
        assert medium.minimum_golden_set == min_n_zero_failures(0.95, 0.95)
        assert high.minimum_golden_set == min_n_zero_failures(0.98, 0.95)


class TestRationale:
    def test_rationale_is_prose_a_reviewer_could_paste_into_a_document(self):
        rationale = assess_risk(make_spec(risk_class=None)).rationale
        assert len(rationale) > 400
        assert "model influence" in rationale.lower()
        assert "GAMP category 5" in rationale
        assert "not regulatory minima" in rationale

    def test_rationale_names_the_agent_and_version(self):
        rationale = assess_risk(make_spec("my-agent", "9.9")).rationale
        assert "my-agent" in rationale and "9.9" in rationale

    def test_rationale_reports_when_no_escalation_applied(self):
        spec = spec_with_context(
            model_influence=RiskLevel.LOW, decision_consequence=RiskLevel.LOW
        )
        spec = spec.replace(gamp=spec.gamp.replace(category=GampCategory.CONFIGURED))
        assert "No escalation rules applied." in assess_risk(spec).rationale
