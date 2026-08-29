"""Model risk assessment.

Risk is the hinge of the whole methodology. It decides how much evidence a
given agent needs, and therefore what the validation costs and how long it
takes. Both FDA's January 2025 draft guidance and GAMP 5 2nd edition are
risk-based frameworks in exactly this sense: the rigour is meant to be
proportionate, not uniform.

The core is the credibility framework's two-axis model:

*Model influence* — how much the agent's output contributes to the decision,
relative to other evidence. An agent whose suggestion a human independently
re-derives has low influence; one whose output is accepted as-is has high
influence.

*Decision consequence* — the severity of an adverse outcome if the decision
turns out to be wrong.

Model risk is the combination. ValKit implements it as an explicit
three-by-three table rather than as arithmetic on ordinal codes, because the
cells are a policy judgement and ought to be readable and arguable as such: a
reviewer can point at a cell and disagree with it, which they cannot do with a
formula.

Escalation rules then apply on top. Each is separately documented and
separately testable, and each only ever raises the class — nothing in this
module can lower risk, which is the property that makes the assessment safe to
automate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    AgentSpec,
    ContextOfUse,
    GampCategory,
    RegulatoryImpact,
    RiskClass,
    RiskLevel,
)

__all__ = ["RiskAssessment", "RequiredRigor", "assess_risk", "RISK_MATRIX", "matrix_rationale"]


# The credibility framework's model-risk matrix, indexed
# [model_influence][decision_consequence].
RISK_MATRIX: dict[RiskLevel, dict[RiskLevel, RiskClass]] = {
    # Low influence: the output is one input among several and is independently
    # checked, so even a severe consequence is mediated by other controls.
    RiskLevel.LOW: {
        RiskLevel.LOW: RiskClass.LOW,
        RiskLevel.MEDIUM: RiskClass.LOW,
        RiskLevel.HIGH: RiskClass.MEDIUM,
    },
    # Medium influence: the output materially shapes the decision. A severe
    # consequence here is high risk even with a reviewer in the loop, because
    # review of a plausible-looking output is weak evidence.
    RiskLevel.MEDIUM: {
        RiskLevel.LOW: RiskClass.LOW,
        RiskLevel.MEDIUM: RiskClass.MEDIUM,
        RiskLevel.HIGH: RiskClass.HIGH,
    },
    # High influence: the output effectively is the decision.
    RiskLevel.HIGH: {
        RiskLevel.LOW: RiskClass.MEDIUM,
        RiskLevel.MEDIUM: RiskClass.HIGH,
        RiskLevel.HIGH: RiskClass.HIGH,
    },
}


_MATRIX_NOTES: dict[tuple[RiskLevel, RiskLevel], str] = {
    (RiskLevel.LOW, RiskLevel.LOW): (
        "The output contributes little to the decision and an error would have minor "
        "consequences."
    ),
    (RiskLevel.LOW, RiskLevel.MEDIUM): (
        "The output is one input among several and is independently corroborated, so a "
        "moderate consequence remains adequately mitigated."
    ),
    (RiskLevel.LOW, RiskLevel.HIGH): (
        "Although influence is low, the consequence of an undetected error is severe, so "
        "the risk cannot be treated as low."
    ),
    (RiskLevel.MEDIUM, RiskLevel.LOW): (
        "The output shapes the decision but an error would be readily absorbed."
    ),
    (RiskLevel.MEDIUM, RiskLevel.MEDIUM): (
        "The output materially shapes a decision whose failure has moderate consequences; "
        "the standard case for a documented acceptance battery."
    ),
    (RiskLevel.MEDIUM, RiskLevel.HIGH): (
        "The output materially shapes a decision with severe consequences. Human review of "
        "a fluent, plausible output is weak evidence and does not by itself reduce this."
    ),
    (RiskLevel.HIGH, RiskLevel.LOW): (
        "The output effectively is the decision, so despite limited consequence the agent "
        "carries the decision alone and warrants more than minimal evidence."
    ),
    (RiskLevel.HIGH, RiskLevel.MEDIUM): (
        "The output effectively is the decision and an error carries real consequence."
    ),
    (RiskLevel.HIGH, RiskLevel.HIGH): (
        "The output effectively is the decision and an error is severe. This is the "
        "highest-rigour case."
    ),
}


def matrix_rationale(influence: RiskLevel, consequence: RiskLevel) -> str:
    """The documented justification for one cell of the matrix."""
    return _MATRIX_NOTES[(influence, consequence)]


@dataclass(frozen=True)
class RequiredRigor:
    """The evidence a given risk class calls for.

    These are ValKit's recommended floors, not regulatory minima; no regulation
    states a golden-set size. They exist so that a team has a defensible
    starting point and so that the validation plan records what was chosen and
    why.
    """

    minimum_golden_set: int
    suggested_target: float
    confidence: float
    judge_calibration_required: bool
    monitoring_required: bool
    red_team_required: bool
    review_months: int
    independent_approver_required: bool
    notes: str = ""


@dataclass(frozen=True)
class RiskAssessment:
    """The outcome of assessing one agent version."""

    risk_class: RiskClass
    derived_class: RiskClass
    """What the matrix and escalations produced, before any spec override."""

    gamp_category: GampCategory
    model_influence: RiskLevel
    decision_consequence: RiskLevel
    regulatory_impact: RegulatoryImpact
    matrix_cell: RiskClass
    """The class before escalation rules, straight from the matrix."""

    escalations: list[str] = field(default_factory=list)
    overridden: bool = False
    rationale: str = ""
    required_rigor: RequiredRigor | None = None


_RIGOR: dict[RiskClass, RequiredRigor] = {
    RiskClass.LOW: RequiredRigor(
        minimum_golden_set=30,
        suggested_target=0.90,
        confidence=0.95,
        judge_calibration_required=False,
        monitoring_required=False,
        red_team_required=False,
        review_months=12,
        independent_approver_required=False,
        notes="Proportionate evidence; unscripted testing may carry much of the assurance.",
    ),
    RiskClass.MEDIUM: RequiredRigor(
        minimum_golden_set=59,
        suggested_target=0.95,
        confidence=0.95,
        judge_calibration_required=True,
        monitoring_required=True,
        red_team_required=True,
        review_months=6,
        independent_approver_required=True,
        notes=(
            "A documented acceptance battery with a stated confidence bound, adversarial "
            "cases, and scheduled re-evaluation."
        ),
    ),
    RiskClass.HIGH: RequiredRigor(
        minimum_golden_set=149,
        suggested_target=0.98,
        confidence=0.95,
        judge_calibration_required=True,
        monitoring_required=True,
        red_team_required=True,
        review_months=3,
        independent_approver_required=True,
        notes=(
            "Full acceptance battery with per-stratum breakdown, calibrated judge, "
            "adversarial testing, continuous monitoring and independent approval."
        ),
    ),
}


def _escalate(current: RiskClass, steps: int = 1) -> RiskClass:
    """Raise a risk class by ``steps``, saturating at HIGH."""
    order = [RiskClass.LOW, RiskClass.MEDIUM, RiskClass.HIGH]
    return order[min(len(order) - 1, order.index(current) + steps)]


def assess_risk(spec: AgentSpec) -> RiskAssessment:
    """Derive the model risk class for an agent specification.

    An explicit ``gamp.risk_class`` in the specification is honoured, because a
    quality function may have information the tool does not. But an override
    that sits *below* the derived class is recorded as such in both the
    escalations list and the rationale, and appears in the generated risk
    assessment document. Silently accepting a lower rating than the evidence
    supports would defeat the purpose of assessing risk at all.
    """
    context: ContextOfUse = spec.context_of_use
    influence = context.model_influence
    consequence = context.decision_consequence

    matrix_cell = RISK_MATRIX[influence][consequence]
    derived = matrix_cell
    escalations: list[str] = []

    # Each factor that fires is recorded whether or not it changes the class.
    # A risk assessment that silently omits "patient safety impact was declared"
    # because the agent was already high risk gives a reviewer no way to see
    # that the factor was considered at all.
    if context.patient_safety_impact:
        if derived is not RiskClass.HIGH:
            escalations.append(
                "Escalated to high: the specification declares patient-safety impact. An "
                "agent that can contribute to patient harm is treated as high risk "
                "regardless of its position in the influence matrix."
            )
        else:
            escalations.append(
                "Patient-safety impact is declared. The classification is already high, "
                "so no further escalation applies, but the declaration is recorded as a "
                "determinant of the class."
            )
        derived = RiskClass.HIGH

    if not context.human_in_the_loop:
        before = derived
        derived = _escalate(derived)
        if derived is not before:
            escalations.append(
                f"Escalated from {before.value} to {derived.value}: no human reviews the "
                "output before it is used, so there is no compensating control between an "
                "erroneous output and its consequence."
            )
        else:
            escalations.append(
                "No human reviews the output before it is used, so no compensating control "
                "stands between an erroneous output and its consequence. The classification "
                "is already high and cannot rise further."
            )

    if context.regulatory_impact is RegulatoryImpact.HIGH:
        before = derived
        derived = _escalate(derived)
        if derived is not before:
            escalations.append(
                f"Escalated from {before.value} to {derived.value}: the output has high "
                "regulatory impact, so an error is likely to reach a regulatory submission "
                "or an inspected record."
            )
        else:
            escalations.append(
                "The output has high regulatory impact. The classification is already high "
                "and cannot rise further."
            )

    if spec.gamp.category is GampCategory.BESPOKE and derived is RiskClass.LOW:
        derived = RiskClass.MEDIUM
        escalations.append(
            "Raised to medium: GAMP category 5 (bespoke) software carries no supplier "
            "assurance and no installed base, so it does not qualify for the lowest tier "
            "of evidence."
        )

    if context.product_quality_impact and derived is RiskClass.LOW:
        derived = RiskClass.MEDIUM
        escalations.append(
            "Raised to medium: the specification declares product-quality impact."
        )

    final = derived
    overridden = False
    if spec.gamp.risk_class is not None and spec.gamp.risk_class is not derived:
        overridden = True
        final = spec.gamp.risk_class
        if spec.gamp.risk_class.rank < derived.rank:
            escalations.append(
                f"OVERRIDE: the specification sets risk_class to "
                f"{spec.gamp.risk_class.value!r}, which is lower than the derived class "
                f"{derived.value!r}. The override is applied but must be justified by the "
                f"quality function in the risk assessment; the derived class is recorded "
                f"here so the difference is visible to a reviewer."
            )
        else:
            escalations.append(
                f"The specification sets risk_class to {spec.gamp.risk_class.value!r}, "
                f"above the derived class {derived.value!r}. The higher class is applied."
            )

    rationale = _build_rationale(
        spec, influence, consequence, matrix_cell, derived, final, escalations, overridden
    )

    return RiskAssessment(
        risk_class=final,
        derived_class=derived,
        gamp_category=spec.gamp.category,
        model_influence=influence,
        decision_consequence=consequence,
        regulatory_impact=context.regulatory_impact,
        matrix_cell=matrix_cell,
        escalations=escalations,
        overridden=overridden,
        rationale=rationale,
        required_rigor=_RIGOR[final],
    )


def _build_rationale(
    spec: AgentSpec,
    influence: RiskLevel,
    consequence: RiskLevel,
    matrix_cell: RiskClass,
    derived: RiskClass,
    final: RiskClass,
    escalations: list[str],
    overridden: bool,
) -> str:
    """Compose the paragraph that goes into the risk assessment document."""
    parts = [
        f"The agent {spec.agent_id} version {spec.version} is used as follows: "
        f"{spec.context_of_use.role} "
        f"Model influence on the decision is assessed as {influence.value}, and the "
        f"consequence of an incorrect decision as {consequence.value}. "
        f"{matrix_rationale(influence, consequence)} "
        f"The influence-by-consequence matrix therefore gives an initial model risk of "
        f"{matrix_cell.value}."
    ]

    if spec.gamp.category is GampCategory.BESPOKE:
        parts.append(
            "The system is GAMP category 5 (bespoke), since the agent's behaviour is "
            "determined by prompts, tools and a model configuration specific to this use."
        )
    else:
        parts.append(f"The system is GAMP category {int(spec.gamp.category.value)}.")

    if escalations:
        parts.append("The following adjustments were applied: " + " ".join(escalations))
    else:
        parts.append("No escalation rules applied.")

    if overridden:
        parts.append(
            f"The specification overrides the derived class of {derived.value}; the applied "
            f"class is {final.value}."
        )
    else:
        parts.append(f"The applied model risk class is {final.value}.")

    rigor = _RIGOR[final]
    parts.append(
        f"At {final.value} risk, ValKit's recommended evidence floor is a curated golden "
        f"set of at least {rigor.minimum_golden_set} representative cases, judge "
        f"calibration {'required' if rigor.judge_calibration_required else 'optional'}, "
        f"adversarial testing {'required' if rigor.red_team_required else 'optional'}, "
        f"scheduled re-evaluation "
        f"{'required' if rigor.monitoring_required else 'optional'}, and periodic review "
        f"every {rigor.review_months} months. These are proportionality recommendations, "
        f"not regulatory minima, and the quality function remains responsible for the "
        f"final determination."
    )
    return " ".join(parts)
