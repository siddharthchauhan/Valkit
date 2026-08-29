"""Tests for change control.

The behaviour that matters most: a change cannot be approved until the
re-evaluation actually covers the scope its impact assessment demanded.
Approving on the strength of a run that did not exercise the affected metric is
the failure mode change control exists to prevent.
"""

from __future__ import annotations

import pytest

from valkit.audit import AuditTrail
from valkit.change.control import (
    ALLOWED_TRANSITIONS,
    REQUIRED_SCOPE,
    ChangeControlRegister,
    changed_metric_names,
    version_diff,
)
from valkit.errors import ChangeControlError
from valkit.models import (
    BoundMethod,
    ChangeControlStatus,
    ChangeTrigger,
    JudgeCalibration,
    MetricResult,
    MetricType,
)
from valkit.testing import make_run, make_spec
from valkit.util import FrozenClock


@pytest.fixture
def register(clock):
    return ChangeControlRegister(clock=clock)


@pytest.fixture
def spec():
    return make_spec()


def run_with(metrics: dict[str, bool], *, run_id="RUN-1", kappa: float | None = None):
    """A run whose named metrics passed or failed as given."""
    run = make_run(run_id=run_id).replace(
        metrics=[
            MetricResult(
                name=name,
                type=MetricType.PROPORTION,
                n=100,
                k=95 if passed else 50,
                point_estimate=0.95 if passed else 0.50,
                method=BoundMethod.CLOPPER_PEARSON_LOWER,
                confidence=0.95,
                passed=passed,
            )
            for name, passed in metrics.items()
        ]
    )
    if kappa is not None:
        run = run.replace(
            calibration=JudgeCalibration(
                judge_model="fixture/judge",
                n=30,
                cohen_kappa=kappa,
                percent_agreement=0.95,
                min_required=0.80,
                passed=kappa >= 0.80,
            )
        )
    return run


class TestOpening:
    def test_opens_with_a_deterministic_identifier(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "model bumped")
        assert record.cc_id == "CC-0001"
        assert record.status is ChangeControlStatus.OPEN

    def test_a_reason_is_required(self, register):
        with pytest.raises(ChangeControlError, match="must state a reason"):
            register.open("agent", "1.0", ChangeTrigger.OTHER, "   ")

    def test_a_string_trigger_is_accepted(self, register):
        record = register.open("agent", "1.0", "drift", "metric dropped")
        assert record.trigger is ChangeTrigger.DRIFT

    def test_unknown_identifier_is_a_clear_error(self, register):
        with pytest.raises(ChangeControlError, match="no change control"):
            register.get("CC-9999")


class TestImpactAssessment:
    def test_every_trigger_has_a_documented_scope(self):
        for trigger in ChangeTrigger:
            code, rationale = REQUIRED_SCOPE[trigger]
            assert code
            assert len(rationale) > 60

    def test_a_model_bump_requires_the_full_battery_and_recalibration(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "sonnet 4 to 4.5")
        assessed = register.assess_impact(record.cc_id, spec)
        assert set(assessed.required_scope) == {
            *(m.name for m in spec.acceptance.metrics),
            "judge_calibration",
        }
        assert "judge is usually the same model family" in assessed.impact

    def test_no_recalibration_when_no_judge_is_configured(self, register, spec):
        without = spec.replace(models=spec.models.replace(judge=None))
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "bump")
        assessed = register.assess_impact(record.cc_id, without)
        assert "judge_calibration" not in assessed.required_scope

    def test_a_drift_trigger_scopes_to_the_affected_metric(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "field_accuracy dropped")
        assessed = register.assess_impact(record.cc_id, spec, metrics=["field_accuracy"])
        assert "field_accuracy" in assessed.required_scope
        assert "citation_accuracy" not in assessed.required_scope

    def test_an_unidentified_affected_set_defaults_to_everything(self, register, spec):
        """Narrowing scope on an unstated assumption is how a change escapes."""
        record = register.open("agent", "1.0", ChangeTrigger.DEFECT, "a defect")
        assessed = register.assess_impact(record.cc_id, spec)
        assert set(assessed.required_scope) == {m.name for m in spec.acceptance.metrics}
        assert "defaults to the full battery" in assessed.impact

    def test_the_scope_assessment_is_retrievable(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "bump")
        register.assess_impact(record.cc_id, spec)
        scope = register.scope(record.cc_id)
        assert scope.judge_recalibration
        assert scope.rationale


class TestStateMachine:
    def test_the_transition_table_is_total(self):
        for status in ChangeControlStatus:
            assert status in ALLOWED_TRANSITIONS

    def test_closed_is_terminal(self):
        assert ALLOWED_TRANSITIONS[ChangeControlStatus.CLOSED] == frozenset()

    def test_cannot_approve_before_assessing_impact(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        with pytest.raises(ChangeControlError, match="cannot be approved"):
            register.approve(record.cc_id)

    def test_cannot_attach_a_run_before_assessing_impact(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        with pytest.raises(ChangeControlError, match="assess its impact first"):
            register.attach_run(record.cc_id, run_with({"field_accuracy": True}))

    def test_cannot_close_before_approving_or_rejecting(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        register.assess_impact(record.cc_id, spec, metrics=["field_accuracy"])
        with pytest.raises(ChangeControlError, match="cannot move from"):
            register.close(record.cc_id)

    def test_an_illegal_transition_names_the_permitted_ones(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        with pytest.raises(ChangeControlError, match="permitted next states are"):
            register.close(record.cc_id)


class TestScopeEnforcement:
    def _assessed(self, register, spec, metrics=("field_accuracy",)):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        return register.assess_impact(record.cc_id, spec, metrics=list(metrics))

    def test_an_uncovered_scope_blocks_completion(self, register, spec):
        record = self._assessed(register, spec)
        register.attach_run(record.cc_id, run_with({"citation_accuracy": True}))
        covered, shortfalls = register.evaluate(
            record.cc_id, [run_with({"citation_accuracy": True})]
        )
        assert not covered
        assert any("no attached run evaluated it" in s for s in shortfalls)

    def test_a_failing_re_evaluation_blocks_completion(self, register, spec):
        record = self._assessed(register, spec)
        run = run_with({"field_accuracy": False})
        register.attach_run(record.cc_id, run)
        covered, shortfalls = register.evaluate(record.cc_id, [run])
        assert not covered
        assert any("did not meet its target" in s for s in shortfalls)

    def test_coverage_and_outcome_are_distinguished(self, register, spec):
        """A metric never re-run is a different problem from one that failed."""
        record = self._assessed(register, spec, metrics=["field_accuracy", "citation_accuracy"])
        run = run_with({"field_accuracy": False})
        register.attach_run(record.cc_id, run)
        _, shortfalls = register.evaluate(record.cc_id, [run])
        assert any("did not meet its target" in s for s in shortfalls)
        assert any("no attached run evaluated it" in s for s in shortfalls)

    def test_a_covered_and_passing_scope_completes(self, register, spec):
        record = self._assessed(register, spec)
        run = run_with({"field_accuracy": True}, kappa=0.9)
        register.attach_run(record.cc_id, run)
        completed = register.complete_evaluation(record.cc_id, [run])
        assert completed.status is ChangeControlStatus.EVAL_COMPLETE

    def test_completion_is_refused_with_an_explanation(self, register, spec):
        record = self._assessed(register, spec)
        run = run_with({"field_accuracy": False})
        register.attach_run(record.cc_id, run)
        with pytest.raises(ChangeControlError, match="did not meet its target"):
            register.complete_evaluation(record.cc_id, [run])

    def test_judge_recalibration_in_scope_must_be_performed(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "bump")
        register.assess_impact(record.cc_id, spec)
        run = run_with({m.name: True for m in spec.acceptance.metrics})
        register.attach_run(record.cc_id, run)
        _, shortfalls = register.evaluate(record.cc_id, [run])
        assert any("no attached run performed it" in s for s in shortfalls)

    def test_a_failed_recalibration_blocks(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.MODEL_VERSION, "bump")
        register.assess_impact(record.cc_id, spec)
        run = run_with({m.name: True for m in spec.acceptance.metrics}, kappa=0.4)
        register.attach_run(record.cc_id, run)
        _, shortfalls = register.evaluate(record.cc_id, [run])
        assert any("Judge calibration failed" in s for s in shortfalls)

    def test_an_unassessed_change_cannot_be_evaluated(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        covered, shortfalls = register.evaluate(record.cc_id, [])
        assert not covered
        assert "has not been assessed" in shortfalls[0]


class TestApprovalAndClosure:
    def _to_complete(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        register.assess_impact(record.cc_id, spec, metrics=["field_accuracy"])
        # The drift trigger scopes in judge recalibration when a judge is
        # configured, so the run has to have performed it.
        run = run_with({"field_accuracy": True}, kappa=0.9)
        register.attach_run(record.cc_id, run)
        register.complete_evaluation(record.cc_id, [run])
        return record.cc_id

    def test_approval_after_a_covered_evaluation(self, register, spec):
        cc_id = self._to_complete(register, spec)
        approved = register.approve(cc_id)
        assert approved.status is ChangeControlStatus.APPROVED
        assert "covered the required scope" in approved.outcome

    def test_rejection_records_the_reason(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        rejected = register.reject(record.cc_id, "Change withdrawn.")
        assert rejected.status is ChangeControlStatus.REJECTED
        assert rejected.outcome == "Change withdrawn."

    def test_closing_records_the_time(self, register, spec):
        cc_id = self._to_complete(register, spec)
        register.approve(cc_id)
        closed = register.close(cc_id)
        assert closed.status is ChangeControlStatus.CLOSED
        assert closed.closed_at


class TestBlocking:
    def test_an_open_change_blocks_the_agent(self, register):
        register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        assert len(register.blocking("agent")) == 1

    def test_an_approved_change_does_not_block(self, register, spec):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        register.assess_impact(record.cc_id, spec, metrics=["field_accuracy"])
        run = run_with({"field_accuracy": True}, kappa=0.9)
        register.attach_run(record.cc_id, run)
        register.complete_evaluation(record.cc_id, [run])
        register.approve(record.cc_id)
        assert register.blocking("agent") == []

    def test_a_rejected_change_does_not_block(self, register):
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        register.reject(record.cc_id, "withdrawn")
        assert register.blocking("agent") == []

    def test_blocking_is_scoped_to_the_agent(self, register):
        register.open("agent-a", "1.0", ChangeTrigger.DRIFT, "drift")
        assert register.blocking("agent-b") == []


class TestPersistence:
    def test_records_survive_a_reload(self, workdir, clock):
        path = workdir / "changes.jsonl"
        first = ChangeControlRegister(path, clock=clock)
        record = first.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")

        second = ChangeControlRegister(path, clock=clock)
        assert second.get(record.cc_id).reason == "drift"

    def test_identifiers_do_not_collide_after_a_reload(self, workdir, clock):
        path = workdir / "changes.jsonl"
        first = ChangeControlRegister(path, clock=clock)
        first.open("agent", "1.0", ChangeTrigger.DRIFT, "one")

        second = ChangeControlRegister(path, clock=clock)
        assert second.open("agent", "1.0", ChangeTrigger.DRIFT, "two").cc_id == "CC-0002"


class TestAudit:
    def test_every_transition_is_audited(self, clock, spec):
        audit = AuditTrail(":memory:", clock)
        register = ChangeControlRegister(clock=clock, audit=audit)
        record = register.open("agent", "1.0", ChangeTrigger.DRIFT, "drift")
        register.assess_impact(record.cc_id, spec, metrics=["field_accuracy"])
        run = run_with({"field_accuracy": True}, kappa=0.9)
        register.attach_run(record.cc_id, run)
        register.complete_evaluation(record.cc_id, [run])
        register.approve(record.cc_id)
        register.close(record.cc_id)

        actions = {r.action for r in audit.records()}
        assert {
            "change_control.opened",
            "change_control.impact_assessed",
            "change_control.run_attached",
            "change_control.evaluation_complete",
            "change_control.approved",
            "change_control.closed",
        } <= actions
        assert audit.verify().ok


class TestVersionDiff:
    def test_no_change_yields_no_trigger(self, spec):
        assert version_diff(spec, spec) == []

    def test_a_model_change_is_detected(self, spec):
        changed = spec.replace(models=spec.models.replace(primary="bedrock/other"))
        assert ChangeTrigger.MODEL_VERSION in version_diff(spec, changed)

    def test_a_judge_change_is_detected(self, spec):
        changed = spec.replace(models=spec.models.replace(judge="bedrock/other-judge"))
        assert ChangeTrigger.MODEL_VERSION in version_diff(spec, changed)

    def test_a_temperature_change_is_detected(self, spec):
        changed = spec.replace(models=spec.models.replace(temperature=0.7))
        assert ChangeTrigger.PROMPT_CHANGE in version_diff(spec, changed)

    def test_a_dataset_change_is_detected(self, spec):
        changed = spec.replace(
            datasets=spec.datasets.replace(
                golden_set=spec.datasets.golden_set.replace(sha256="a" * 64)
            )
        )
        assert ChangeTrigger.DATASET_CHANGE in version_diff(spec, changed)

    def test_a_target_change_is_detected(self, spec):
        metrics = [m.replace(target=0.99) for m in spec.acceptance.metrics]
        changed = spec.replace(acceptance=spec.acceptance.replace(metrics=metrics))
        assert ChangeTrigger.SPEC_CHANGE in version_diff(spec, changed)

    def test_a_context_of_use_change_is_detected(self, spec):
        changed = spec.replace(
            context_of_use=spec.context_of_use.replace(human_in_the_loop=False)
        )
        assert ChangeTrigger.SPEC_CHANGE in version_diff(spec, changed)

    def test_triggers_are_not_duplicated(self, spec):
        changed = spec.replace(
            context_of_use=spec.context_of_use.replace(human_in_the_loop=False),
            intended_use=spec.intended_use.replace(in_scope=["something else"]),
        )
        triggers = version_diff(spec, changed)
        assert len(triggers) == len(set(triggers))

    def test_changed_metric_names(self, spec):
        metrics = list(spec.acceptance.metrics)
        metrics[0] = metrics[0].replace(target=0.99)
        changed = spec.replace(acceptance=spec.acceptance.replace(metrics=metrics))
        assert changed_metric_names(spec, changed) == [metrics[0].name]

    def test_a_removed_metric_is_reported(self, spec):
        changed = spec.replace(
            acceptance=spec.acceptance.replace(metrics=list(spec.acceptance.metrics)[:1])
        )
        removed = changed_metric_names(spec, changed)
        assert spec.acceptance.metrics[1].name in removed
