"""Change control.

Once an agent is validated, any change to the model, the prompt, the
qualification data or the specification puts that status in question. Change
control is the discipline that answers, before the change ships: what changed,
what does it put at risk, what has to be re-demonstrated, and who agreed.

The part that carries the weight is :meth:`ChangeControlRegister.assess_impact`,
which derives the required re-evaluation scope from the trigger. It is an
explicit table rather than a judgement, so that the scope for a given kind of
change is the same every time and can be argued with. A model version bump
requires the full battery *and* judge recalibration, because the judge is
usually the same family of model and its behaviour moves too — that is the case
teams most often miss.

The state machine refuses to approve a change whose required scope has not been
covered by a passing run. Approving on the strength of a re-evaluation that did
not actually cover the affected metric is the failure mode change control exists
to prevent, so it is enforced rather than documented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..audit.store import AuditTrail
from ..errors import ChangeControlError
from ..models import (
    AgentSpec,
    ChangeControl,
    ChangeControlStatus,
    ChangeTrigger,
    EvalRun,
    Signature,
)
from ..util import Clock, SystemClock

__all__ = ["ChangeControlRegister", "version_diff", "REQUIRED_SCOPE", "ALLOWED_TRANSITIONS"]


# What each trigger obliges you to re-demonstrate. "ALL" means every acceptance
# metric in the specification.
REQUIRED_SCOPE: dict[ChangeTrigger, tuple[str, str]] = {
    ChangeTrigger.MODEL_VERSION: (
        "ALL+JUDGE",
        "A change of model version can move behaviour anywhere, so the full acceptance "
        "battery is re-executed. Judge calibration is repeated as well: the judge is "
        "usually the same model family, its agreement with human assessment can move with "
        "it, and a miscalibrated judge would report the re-evaluation incorrectly.",
    ),
    ChangeTrigger.PROMPT_CHANGE: (
        "ALL",
        "A prompt change alters the agent's behaviour by construction. Since the effect "
        "cannot be localised by inspection, the full battery is re-executed.",
    ),
    ChangeTrigger.DATASET_CHANGE: (
        "ALL",
        "A change to the qualification set changes what every metric was measured "
        "against, so no prior result carries over.",
    ),
    ChangeTrigger.SPEC_CHANGE: (
        "AFFECTED",
        "Only the metrics whose definition, target, confidence or method changed need "
        "re-demonstration; results for untouched criteria remain valid against unchanged "
        "inputs.",
    ),
    ChangeTrigger.DRIFT: (
        "AFFECTED+JUDGE",
        "The metric that tripped is re-demonstrated. Judge calibration is included where "
        "a judge is configured, since a shift in measured performance may be a shift in "
        "the instrument rather than in the agent.",
    ),
    ChangeTrigger.DEFECT: (
        "AFFECTED",
        "The metrics implicated by the defect are re-demonstrated, together with any "
        "metric the corrective action could plausibly affect.",
    ),
    ChangeTrigger.PERIODIC_REVIEW: (
        "ALL",
        "Periodic review re-establishes the whole claim rather than part of it.",
    ),
    ChangeTrigger.OTHER: (
        "ALL",
        "The scope of an unclassified change cannot be bounded by rule, so the full "
        "battery is re-executed unless the quality function narrows it with a recorded "
        "rationale.",
    ),
}


ALLOWED_TRANSITIONS: dict[ChangeControlStatus, frozenset[ChangeControlStatus]] = {
    ChangeControlStatus.OPEN: frozenset(
        {ChangeControlStatus.IMPACT_ASSESSED, ChangeControlStatus.REJECTED}
    ),
    ChangeControlStatus.IMPACT_ASSESSED: frozenset(
        {ChangeControlStatus.EVAL_IN_PROGRESS, ChangeControlStatus.REJECTED}
    ),
    ChangeControlStatus.EVAL_IN_PROGRESS: frozenset(
        {
            ChangeControlStatus.EVAL_IN_PROGRESS,
            ChangeControlStatus.EVAL_COMPLETE,
            ChangeControlStatus.REJECTED,
        }
    ),
    ChangeControlStatus.EVAL_COMPLETE: frozenset(
        {
            ChangeControlStatus.EVAL_IN_PROGRESS,
            ChangeControlStatus.APPROVED,
            ChangeControlStatus.REJECTED,
        }
    ),
    ChangeControlStatus.APPROVED: frozenset({ChangeControlStatus.CLOSED}),
    ChangeControlStatus.REJECTED: frozenset({ChangeControlStatus.CLOSED}),
    ChangeControlStatus.CLOSED: frozenset(),
}


@dataclass
class ScopeAssessment:
    """The required re-evaluation scope and the reasoning behind it."""

    metrics: list[str]
    judge_recalibration: bool
    rationale: str


class ChangeControlRegister:
    """Opens, advances and closes change control records."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Clock | None = None,
        audit: AuditTrail | None = None,
    ):
        self._clock = clock or SystemClock()
        self._audit = audit
        self._path = Path(path) if path else None
        self._records: dict[str, ChangeControl] = {}
        self._scopes: dict[str, ScopeAssessment] = {}
        self._counter = 0
        if self._path is not None and self._path.exists():
            self._load()

    # -- lifecycle ---------------------------------------------------------

    def open(
        self,
        agent_id: str,
        agent_version: str,
        trigger: ChangeTrigger | str,
        reason: str,
        *,
        prior_version: str | None = None,
        new_version: str | None = None,
        cc_id: str | None = None,
    ) -> ChangeControl:
        """Open a change control."""
        trigger = ChangeTrigger(trigger) if isinstance(trigger, str) else trigger
        if not reason.strip():
            raise ChangeControlError(
                "a change control must state a reason; an unexplained change cannot be "
                "assessed for impact"
            )

        self._counter += 1
        record = ChangeControl(
            cc_id=cc_id or f"CC-{self._counter:04d}",
            agent_id=agent_id,
            agent_version=agent_version,
            trigger=trigger,
            reason=reason,
            opened_at=self._clock.now_iso(),
            status=ChangeControlStatus.OPEN,
            prior_version=prior_version,
            new_version=new_version,
        )
        self._records[record.cc_id] = record
        self._persist()
        self._log(record, "change_control.opened", {"trigger": trigger.value, "reason": reason})
        return record

    def get(self, cc_id: str) -> ChangeControl:
        try:
            return self._records[cc_id]
        except KeyError:
            raise ChangeControlError(f"no change control with identifier {cc_id!r}") from None

    def assess_impact(
        self,
        cc_id: str,
        spec: AgentSpec | None = None,
        *,
        metrics: Sequence[str] | None = None,
    ) -> ChangeControl:
        """Derive and record the required re-evaluation scope."""
        record = self.get(cc_id)
        self._require_transition(record, ChangeControlStatus.IMPACT_ASSESSED)

        code, rationale = REQUIRED_SCOPE[record.trigger]
        all_metrics = [m.name for m in spec.acceptance.metrics] if spec else []
        judge_configured = bool(spec and spec.models.judge)

        if code.startswith("ALL"):
            scope_metrics = all_metrics or list(metrics or [])
        else:
            scope_metrics = list(metrics or [])
            if not scope_metrics:
                # Without a stated set of affected metrics the safe reading is
                # that everything is affected. Narrowing scope on the basis of
                # an unstated assumption is how a change escapes assessment.
                scope_metrics = all_metrics
                rationale += (
                    " No affected metrics were identified, so the scope defaults to the "
                    "full battery."
                )

        judge = code.endswith("JUDGE") and judge_configured
        assessment = ScopeAssessment(
            metrics=sorted(set(scope_metrics)), judge_recalibration=judge, rationale=rationale
        )
        self._scopes[cc_id] = assessment

        required = list(assessment.metrics)
        if judge:
            required.append("judge_calibration")

        updated = record.replace(
            status=ChangeControlStatus.IMPACT_ASSESSED,
            impact=rationale,
            required_scope=required,
        )
        self._records[cc_id] = updated
        self._persist()
        self._log(updated, "change_control.impact_assessed", {"required_scope": required})
        return updated

    def attach_run(self, cc_id: str, run: EvalRun) -> ChangeControl:
        """Record a re-evaluation against the change."""
        record = self.get(cc_id)
        if record.status is ChangeControlStatus.IMPACT_ASSESSED:
            self._require_transition(record, ChangeControlStatus.EVAL_IN_PROGRESS)
            record = record.replace(status=ChangeControlStatus.EVAL_IN_PROGRESS)
        elif record.status is not ChangeControlStatus.EVAL_IN_PROGRESS:
            raise ChangeControlError(
                f"cannot attach a run to change control {cc_id} in state "
                f"{record.status.value}; assess its impact first"
            )

        updated = record.replace(run_ids=[*record.run_ids, run.run_id])
        self._records[cc_id] = updated
        self._persist()
        self._log(updated, "change_control.run_attached", {"run_id": run.run_id})
        return updated

    def evaluate(self, cc_id: str, runs: Sequence[EvalRun]) -> tuple[bool, list[str]]:
        """Whether the attached runs cover the required scope and passed.

        Returns the verdict and the list of shortfalls. Coverage and outcome
        are checked separately: a metric that was not re-run at all is a
        different problem from one that was re-run and failed, and conflating
        them would let the first hide behind the second.
        """
        record = self.get(cc_id)
        attached = [r for r in runs if r.run_id in record.run_ids]
        shortfalls: list[str] = []

        if not record.required_scope:
            shortfalls.append("The required re-evaluation scope has not been assessed.")
            return False, shortfalls

        covered: dict[str, bool] = {}
        judge_covered = False
        for run in attached:
            for metric in run.metrics:
                covered[metric.name] = covered.get(metric.name, True) and metric.passed
            if run.calibration is not None:
                judge_covered = True
                if not run.calibration.passed:
                    shortfalls.append(
                        f"Judge calibration failed in run {run.run_id} "
                        f"(kappa {run.calibration.cohen_kappa:.3f})."
                    )

        for required in record.required_scope:
            if required == "judge_calibration":
                if not judge_covered:
                    shortfalls.append(
                        "Judge calibration is in scope but no attached run performed it."
                    )
                continue
            if required not in covered:
                shortfalls.append(
                    f"{required} is in the required scope but no attached run evaluated it."
                )
            elif not covered[required]:
                shortfalls.append(f"{required} was re-evaluated and did not meet its target.")

        return not shortfalls, shortfalls

    def complete_evaluation(self, cc_id: str, runs: Sequence[EvalRun]) -> ChangeControl:
        """Mark the re-evaluation complete, if it actually covers the scope."""
        record = self.get(cc_id)
        covered, shortfalls = self.evaluate(cc_id, runs)
        if not covered:
            raise ChangeControlError(
                f"change control {cc_id} cannot be marked evaluated: "
                + " ".join(shortfalls)
            )
        self._require_transition(record, ChangeControlStatus.EVAL_COMPLETE)
        updated = record.replace(status=ChangeControlStatus.EVAL_COMPLETE)
        self._records[cc_id] = updated
        self._persist()
        self._log(updated, "change_control.evaluation_complete", {"runs": list(record.run_ids)})
        return updated

    def approve(
        self, cc_id: str, signature: Signature | None = None, *, outcome: str = ""
    ) -> ChangeControl:
        """Approve the change. Refuses while the required scope is uncovered."""
        record = self.get(cc_id)
        if record.status is not ChangeControlStatus.EVAL_COMPLETE:
            raise ChangeControlError(
                f"change control {cc_id} is in state {record.status.value} and cannot be "
                f"approved: the required re-evaluation must be completed and shown to "
                f"cover the assessed scope first"
            )
        self._require_transition(record, ChangeControlStatus.APPROVED)
        updated = record.replace(
            status=ChangeControlStatus.APPROVED,
            signatures=[*record.signatures, signature] if signature else record.signatures,
            outcome=outcome or "Re-evaluation covered the required scope and passed.",
        )
        self._records[cc_id] = updated
        self._persist()
        self._log(
            updated,
            "change_control.approved",
            {"signature_id": signature.signature_id if signature else None},
        )
        return updated

    def reject(
        self, cc_id: str, reason: str, signature: Signature | None = None
    ) -> ChangeControl:
        record = self.get(cc_id)
        self._require_transition(record, ChangeControlStatus.REJECTED)
        updated = record.replace(
            status=ChangeControlStatus.REJECTED,
            outcome=reason,
            signatures=[*record.signatures, signature] if signature else record.signatures,
        )
        self._records[cc_id] = updated
        self._persist()
        self._log(updated, "change_control.rejected", {"reason": reason})
        return updated

    def close(self, cc_id: str) -> ChangeControl:
        record = self.get(cc_id)
        self._require_transition(record, ChangeControlStatus.CLOSED)
        updated = record.replace(
            status=ChangeControlStatus.CLOSED, closed_at=self._clock.now_iso()
        )
        self._records[cc_id] = updated
        self._persist()
        self._log(updated, "change_control.closed", {})
        return updated

    # -- queries -----------------------------------------------------------

    def all(self) -> list[ChangeControl]:
        return sorted(self._records.values(), key=lambda r: r.cc_id)

    def open_for_agent(self, agent_id: str) -> list[ChangeControl]:
        return [
            r
            for r in self.all()
            if r.agent_id == agent_id
            and r.status not in (ChangeControlStatus.CLOSED, ChangeControlStatus.REJECTED)
        ]

    def blocking(self, agent_id: str) -> list[ChangeControl]:
        """Open change controls that prevent the agent counting as validated.

        A change that has been approved but not yet closed is not blocking: the
        re-evaluation covered its scope and the quality function signed. A
        change still awaiting evidence or approval is.
        """
        return [
            r
            for r in self.open_for_agent(agent_id)
            if r.status
            not in (
                ChangeControlStatus.APPROVED,
                ChangeControlStatus.CLOSED,
                ChangeControlStatus.REJECTED,
            )
        ]

    def scope(self, cc_id: str) -> ScopeAssessment | None:
        return self._scopes.get(cc_id)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _require_transition(record: ChangeControl, target: ChangeControlStatus) -> None:
        allowed = ALLOWED_TRANSITIONS[record.status]
        if target not in allowed:
            permitted = ", ".join(sorted(s.value for s in allowed)) or "nothing"
            raise ChangeControlError(
                f"change control {record.cc_id} cannot move from {record.status.value} to "
                f"{target.value}; permitted next states are {permitted}"
            )

    def _log(self, record: ChangeControl, action: str, payload: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit.append(
                actor="system",
                action=action,
                entity_type="change_control",
                entity_id=record.cc_id,
                payload={"status": record.status.value, **payload},
            )

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            "".join(record.to_json() + "\n" for record in self.all()), encoding="utf-8"
        )

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            record = ChangeControl(
                cc_id=raw["cc_id"],
                agent_id=raw["agent_id"],
                agent_version=raw["agent_version"],
                trigger=ChangeTrigger(raw["trigger"]),
                reason=raw["reason"],
                opened_at=raw["opened_at"],
                status=ChangeControlStatus(raw["status"]),
                impact=raw.get("impact", ""),
                required_scope=raw.get("required_scope", []),
                run_ids=raw.get("run_ids", []),
                closed_at=raw.get("closed_at"),
                outcome=raw.get("outcome", ""),
                prior_version=raw.get("prior_version"),
                new_version=raw.get("new_version"),
            )
            self._records[record.cc_id] = record
            number = int(record.cc_id.rsplit("-", 1)[-1]) if "-" in record.cc_id else 0
            self._counter = max(self._counter, number)


def version_diff(old: AgentSpec, new: AgentSpec) -> list[ChangeTrigger]:
    """The change triggers implied by a difference between two specifications.

    Lets a CI job open the right change control from a diff, rather than
    relying on someone remembering to.
    """
    triggers: list[ChangeTrigger] = []

    if old.models.primary != new.models.primary or old.models.judge != new.models.judge:
        triggers.append(ChangeTrigger.MODEL_VERSION)
    if (
        old.models.temperature != new.models.temperature
        or old.models.seed != new.models.seed
        or old.models.parameters != new.models.parameters
    ):
        triggers.append(ChangeTrigger.PROMPT_CHANGE)

    def dataset_ref(spec: AgentSpec) -> tuple:
        golden = spec.datasets.golden_set
        red = spec.datasets.red_team
        return (
            (golden.ref, golden.sha256) if golden else None,
            (red.ref, red.sha256) if red else None,
        )

    if dataset_ref(old) != dataset_ref(new):
        triggers.append(ChangeTrigger.DATASET_CHANGE)

    if [m.to_dict() for m in old.acceptance.metrics] != [
        m.to_dict() for m in new.acceptance.metrics
    ]:
        triggers.append(ChangeTrigger.SPEC_CHANGE)
    if old.context_of_use.to_dict() != new.context_of_use.to_dict():
        triggers.append(ChangeTrigger.SPEC_CHANGE)
    if old.intended_use.to_dict() != new.intended_use.to_dict():
        triggers.append(ChangeTrigger.SPEC_CHANGE)

    seen: list[ChangeTrigger] = []
    for trigger in triggers:
        if trigger not in seen:
            seen.append(trigger)
    return seen


def changed_metric_names(old: AgentSpec, new: AgentSpec) -> list[str]:
    """Metric names whose definition differs between two specifications."""
    before = {m.name: m.to_dict() for m in old.acceptance.metrics}
    after = {m.name: m.to_dict() for m in new.acceptance.metrics}
    changed = [name for name, value in after.items() if before.get(name) != value]
    changed.extend(name for name in before if name not in after)
    return sorted(set(changed))
