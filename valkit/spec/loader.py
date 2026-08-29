"""Loading and validating ``valkit.yaml``.

The specification is the contract between an engineering team and their
quality function: it states what the agent is for, how much it is trusted, what
evidence will be gathered and what will count as acceptable. Because every
downstream document quotes it, a mistake here propagates into a signed record,
so this module is deliberately strict and deliberately verbose about failures.

Two design choices follow from that:

*Every error names its path.* ``acceptance.metrics[1].confidence`` tells the
author exactly which line to fix. A validation engineer editing a spec at four
in the afternoon before a release should never have to guess.

*Unknown keys are rejected by default.* A silently ignored ``tolarance_abs``
would leave the author believing a tolerance was in force when none was, and
the resulting acceptance claim would be wrong in a way no test would catch.
Strictness can be relaxed to a warning for exploratory work, but not silently.

The module also returns warnings for constructs that are legal but weaken the
resulting package — most importantly a golden set with no pinned digest, which
means the run cannot be reproduced and the OQ therefore cannot be repeated.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TypeVar

import yaml

from ..errors import SpecError
from ..models import (
    AcceptanceSpec,
    AgentSpec,
    BoundMethod,
    ContextOfUse,
    DatasetSpec,
    DatasetsSpec,
    GampCategory,
    GampSpec,
    IntendedUse,
    JudgeCalibrationSpec,
    MetricSpec,
    MetricType,
    ModelsSpec,
    MonitoringSpec,
    RegulatoryImpact,
    RiskClass,
    RiskLevel,
    SignoffSpec,
    SpecMetadata,
)
from ..util import sha256_text

__all__ = [
    "SpecLoadResult",
    "parse_spec",
    "load_spec",
    "load_spec_from_string",
    "dump_spec",
    "SUPPORTED_API_VERSIONS",
    "SUPPORTED_KINDS",
]

SUPPORTED_API_VERSIONS = ("valkit/v1",)
SUPPORTED_KINDS = ("AgentValidation",)

_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")

E = TypeVar("E")


@dataclass
class SpecLoadResult:
    """A parsed specification together with anything questionable about it."""

    spec: AgentSpec
    warnings: list[str] = field(default_factory=list)
    source: str = ""


# --------------------------------------------------------------------------
# Primitive accessors, each raising SpecError with a precise path
# --------------------------------------------------------------------------


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SpecError(f"expected a mapping, got {type(value).__name__}", path=path)
    return dict(value)


def _reject_unknown(
    data: Mapping[str, Any], known: Sequence[str], path: str, strict: bool, warnings: list[str]
) -> None:
    unknown = sorted(set(data) - set(known))
    if not unknown:
        return
    message = (
        f"unknown key(s) {', '.join(repr(k) for k in unknown)}; expected one of "
        f"{', '.join(repr(k) for k in sorted(known))}"
    )
    if strict:
        raise SpecError(message, path=path)
    warnings.append(f"{path}: {message}")


def _string(data: Mapping[str, Any], key: str, path: str, *, required: bool = False,
            default: str = "") -> str:
    value = data.get(key, None)
    if value is None:
        if required:
            raise SpecError("is required", path=f"{path}.{key}")
        return default
    if not isinstance(value, str):
        # YAML happily yields ints and floats for things like a version of 2.3;
        # coerce rather than reject, since the author's intent is unambiguous.
        value = str(value)
    text = value.strip()
    if required and not text:
        raise SpecError("must not be empty", path=f"{path}.{key}")
    return text


def _bool(data: Mapping[str, Any], key: str, path: str, default: bool) -> bool:
    value = data.get(key, None)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SpecError(f"expected true or false, got {value!r}", path=f"{path}.{key}")
    return value


def _number(
    data: Mapping[str, Any],
    key: str,
    path: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_min: bool = False,
    exclusive_max: bool = False,
) -> float | None:
    value = data.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"expected a number, got {value!r}", path=f"{path}.{key}")
    number = float(value)
    if minimum is not None:
        if exclusive_min and number <= minimum:
            raise SpecError(f"must be greater than {minimum}, got {number}", path=f"{path}.{key}")
        if not exclusive_min and number < minimum:
            raise SpecError(f"must be at least {minimum}, got {number}", path=f"{path}.{key}")
    if maximum is not None:
        if exclusive_max and number >= maximum:
            raise SpecError(f"must be less than {maximum}, got {number}", path=f"{path}.{key}")
        if not exclusive_max and number > maximum:
            raise SpecError(f"must be at most {maximum}, got {number}", path=f"{path}.{key}")
    return number


def _integer(data: Mapping[str, Any], key: str, path: str, *, default: int | None = None,
             minimum: int | None = None) -> int | None:
    value = data.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"expected an integer, got {value!r}", path=f"{path}.{key}")
    if minimum is not None and value < minimum:
        raise SpecError(f"must be at least {minimum}, got {value}", path=f"{path}.{key}")
    return value


def _string_list(data: Mapping[str, Any], key: str, path: str) -> list[str]:
    value = data.get(key, None)
    if value is None:
        return []
    if isinstance(value, str):
        # A single string where a list was expected is a common and harmless
        # slip; accept it as a one-element list.
        return [value.strip()]
    if not isinstance(value, Sequence):
        raise SpecError(f"expected a list of strings, got {type(value).__name__}",
                        path=f"{path}.{key}")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, (str, int, float)):
            raise SpecError(
                f"expected a string, got {type(item).__name__}", path=f"{path}.{key}[{index}]"
            )
        out.append(str(item).strip())
    return out


def _enum(value: Any, enum_cls: type[E], path: str, default: E | None = None) -> E:
    """Coerce a YAML scalar to an enum member, case-insensitively.

    A wrong enum value is one of the most likely authoring mistakes, so the
    message lists every accepted value rather than merely rejecting the input.
    """
    if value is None:
        if default is not None:
            return default
        raise SpecError("is required", path=path)
    if isinstance(value, enum_cls):
        return value

    members = list(enum_cls)  # type: ignore[call-overload]
    if isinstance(value, bool):
        raise SpecError(f"expected one of {_enum_options(members)}, got {value!r}", path=path)

    if isinstance(value, int):
        for member in members:
            if member.value == value:
                return member
    text = str(value).strip().lower()
    for member in members:
        if str(member.value).lower() == text or member.name.lower() == text:
            return member

    raise SpecError(f"expected one of {_enum_options(members)}, got {value!r}", path=path)


def _enum_options(members: Sequence[Any]) -> str:
    return ", ".join(repr(m.value) for m in members)


# --------------------------------------------------------------------------
# Cron
# --------------------------------------------------------------------------


def _validate_cron(expression: str, path: str) -> None:
    """Check the shape of a five-field cron expression.

    Full semantics live in :mod:`valkit.drift`; this is the authoring check, so
    that a four-field expression is rejected at load time rather than silently
    never firing.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise SpecError(
            f"expected a 5-field cron expression "
            f"(minute hour day-of-month month day-of-week), got {len(fields)} field(s): "
            f"{expression!r}",
            path=path,
        )
    for index, part in enumerate(fields):
        if not re.fullmatch(r"[0-9*,/\-A-Za-z]+", part):
            raise SpecError(
                f"field {index + 1} of the cron expression is not valid: {part!r}", path=path
            )


# --------------------------------------------------------------------------
# Block parsers
# --------------------------------------------------------------------------

_METADATA_KEYS = ("agent_id", "version", "owner", "system_of_record", "description", "labels")


def _parse_metadata(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> SpecMetadata:
    path = "metadata"
    block = _require_mapping(data.get("metadata"), path)
    if not block:
        raise SpecError("is required", path=path)
    _reject_unknown(block, _METADATA_KEYS, path, strict, warnings)

    agent_id = _string(block, "agent_id", path, required=True)
    if not _AGENT_ID_PATTERN.match(agent_id):
        raise SpecError(
            "must be 2-64 characters of lowercase letters, digits, dot, underscore or hyphen, "
            f"starting and ending alphanumerically; got {agent_id!r}",
            path=f"{path}.agent_id",
        )

    labels_raw = _require_mapping(block.get("labels"), f"{path}.labels")
    labels = {str(k): str(v) for k, v in labels_raw.items()}

    return SpecMetadata(
        agent_id=agent_id,
        version=_string(block, "version", path, required=True),
        owner=_string(block, "owner", path),
        system_of_record=_string(block, "system_of_record", path),
        description=_string(block, "description", path),
        labels=labels,
    )


_COU_KEYS = (
    "question_of_interest",
    "role",
    "model_influence",
    "decision_consequence",
    "regulatory_impact",
    "human_in_the_loop",
    "patient_safety_impact",
    "product_quality_impact",
    "data_integrity_impact",
)


def _parse_context_of_use(
    data: Mapping[str, Any], strict: bool, warnings: list[str]
) -> ContextOfUse:
    path = "context_of_use"
    block = _require_mapping(data.get("context_of_use"), path)
    if not block:
        raise SpecError(
            "is required: the context of use is step 2 of the FDA credibility "
            "framework and determines the model risk",
            path=path,
        )
    _reject_unknown(block, _COU_KEYS, path, strict, warnings)

    return ContextOfUse(
        question_of_interest=_string(block, "question_of_interest", path, required=True),
        role=_string(block, "role", path, required=True),
        model_influence=_enum(
            block.get("model_influence"), RiskLevel, f"{path}.model_influence", RiskLevel.MEDIUM
        ),
        decision_consequence=_enum(
            block.get("decision_consequence"),
            RiskLevel,
            f"{path}.decision_consequence",
            RiskLevel.MEDIUM,
        ),
        regulatory_impact=_enum(
            block.get("regulatory_impact"),
            RegulatoryImpact,
            f"{path}.regulatory_impact",
            RegulatoryImpact.MEDIUM,
        ),
        human_in_the_loop=_bool(block, "human_in_the_loop", path, True),
        patient_safety_impact=_bool(block, "patient_safety_impact", path, False),
        product_quality_impact=_bool(block, "product_quality_impact", path, False),
        data_integrity_impact=_bool(block, "data_integrity_impact", path, True),
    )


_INTENDED_USE_KEYS = ("in_scope", "out_of_scope", "users", "limitations")


def _parse_intended_use(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> IntendedUse:
    path = "intended_use"
    block = _require_mapping(data.get("intended_use"), path)
    _reject_unknown(block, _INTENDED_USE_KEYS, path, strict, warnings)
    return IntendedUse(
        in_scope=_string_list(block, "in_scope", path),
        out_of_scope=_string_list(block, "out_of_scope", path),
        users=_string_list(block, "users", path),
        limitations=_string_list(block, "limitations", path),
    )


_GAMP_KEYS = ("category", "risk_class", "rationale")


def _parse_gamp(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> GampSpec:
    path = "gamp"
    block = _require_mapping(data.get("gamp"), path)
    _reject_unknown(block, _GAMP_KEYS, path, strict, warnings)

    category = _enum(
        block.get("category"), GampCategory, f"{path}.category", GampCategory.BESPOKE
    )
    risk_class = (
        _enum(block.get("risk_class"), RiskClass, f"{path}.risk_class")
        if block.get("risk_class") is not None
        else None
    )
    return GampSpec(
        category=category,
        risk_class=risk_class,
        rationale=_string(block, "rationale", path),
    )


_MODELS_KEYS = (
    "primary",
    "judge",
    "phi_safe_local",
    "temperature",
    "seed",
    "max_tokens",
    "parameters",
)


def _parse_models(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> ModelsSpec:
    path = "models"
    block = _require_mapping(data.get("models"), path)
    _reject_unknown(block, _MODELS_KEYS, path, strict, warnings)

    temperature = _number(block, "temperature", path, default=0.0, minimum=0.0, maximum=2.0)
    if temperature and temperature > 0.0:
        warnings.append(
            f"{path}.temperature: a non-zero temperature ({temperature}) makes the run "
            "non-reproducible; an OQ executed at temperature > 0 cannot be repeated exactly, "
            "and the validation plan must justify it"
        )

    return ModelsSpec(
        primary=_string(block, "primary", path, required=True),
        judge=_string(block, "judge", path) or None,
        phi_safe_local=_string(block, "phi_safe_local", path) or None,
        temperature=temperature or 0.0,
        seed=_integer(block, "seed", path, default=0),
        max_tokens=_integer(block, "max_tokens", path, minimum=1),
        parameters=_require_mapping(block.get("parameters"), f"{path}.parameters"),
    )


_DATASET_KEYS = ("ref", "sha256", "version", "description")
_DATASETS_KEYS = ("golden_set", "red_team", "calibration_set", "additional")


def _parse_dataset(value: Any, path: str, strict: bool, warnings: list[str]) -> DatasetSpec | None:
    """Accept either a bare reference string or a full mapping."""
    if value is None:
        return None
    if isinstance(value, str):
        return DatasetSpec(ref=value.strip())
    block = _require_mapping(value, path)
    _reject_unknown(block, _DATASET_KEYS, path, strict, warnings)
    digest = _string(block, "sha256", path)
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise SpecError(
            f"must be a 64-character hex SHA-256 digest, got {digest!r}", path=f"{path}.sha256"
        )
    return DatasetSpec(
        ref=_string(block, "ref", path, required=True),
        sha256=digest.lower() or None,
        version=_string(block, "version", path) or None,
        description=_string(block, "description", path),
    )


def _parse_datasets(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> DatasetsSpec:
    path = "datasets"
    block = _require_mapping(data.get("datasets"), path)
    _reject_unknown(block, _DATASETS_KEYS, path, strict, warnings)

    additional_raw = _require_mapping(block.get("additional"), f"{path}.additional")
    additional = {}
    for name, value in additional_raw.items():
        parsed = _parse_dataset(value, f"{path}.additional.{name}", strict, warnings)
        if parsed is not None:
            additional[str(name)] = parsed

    return DatasetsSpec(
        golden_set=_parse_dataset(block.get("golden_set"), f"{path}.golden_set", strict, warnings),
        red_team=_parse_dataset(block.get("red_team"), f"{path}.red_team", strict, warnings),
        calibration_set=_parse_dataset(
            block.get("calibration_set"), f"{path}.calibration_set", strict, warnings
        ),
        additional=additional,
    )


_METRIC_KEYS = (
    "name",
    "type",
    "target",
    "confidence",
    "method",
    "scorer",
    "tolerance_abs",
    "tolerance_rel",
    "max_failures",
    "max_count",
    "baseline",
    "margin",
    "strata",
    "critical",
    "description",
)
_ACCEPTANCE_KEYS = ("metrics", "judge_calibration")
_JUDGE_KEYS = ("min_cohen_kappa", "min_percent_agreement", "min_samples", "required")


def _parse_metric(value: Any, path: str, strict: bool, warnings: list[str]) -> MetricSpec:
    block = _require_mapping(value, path)
    _reject_unknown(block, _METRIC_KEYS, path, strict, warnings)

    name = _string(block, "name", path, required=True)
    metric_type = _enum(block.get("type"), MetricType, f"{path}.type", MetricType.PROPORTION)
    method = _enum(
        block.get("method"), BoundMethod, f"{path}.method", BoundMethod.CLOPPER_PEARSON_LOWER
    )

    target = _number(block, "target", path, minimum=0.0, maximum=1.0,
                     exclusive_min=True, exclusive_max=False)
    if target is not None and target >= 1.0:
        raise SpecError(
            "must be less than 1.0: a target of 1.0 asserts perfection, which no finite "
            "sample can demonstrate",
            path=f"{path}.target",
        )

    confidence = _number(
        block, "confidence", path, default=0.95, minimum=0.0, maximum=1.0,
        exclusive_min=True, exclusive_max=True,
    )

    tolerance_abs = _number(block, "tolerance_abs", path, minimum=0.0)
    tolerance_rel = _number(block, "tolerance_rel", path, minimum=0.0)
    baseline = _number(block, "baseline", path, minimum=0.0, maximum=1.0)
    margin = _number(block, "margin", path, minimum=0.0, maximum=1.0)
    max_count = _integer(block, "max_count", path, minimum=0)

    # Type-specific completeness. Each of these would otherwise surface as a
    # confusing failure at evaluation time, after the model calls have been paid for.
    if metric_type is MetricType.NUMERIC_TOLERANCE and tolerance_abs is None and tolerance_rel is None:
        raise SpecError(
            "a numeric_tolerance metric must set tolerance_abs or tolerance_rel",
            path=path,
        )
    if metric_type is MetricType.COUNT and max_count is None:
        raise SpecError("a count metric must set max_count", path=path)
    if method is BoundMethod.NON_INFERIORITY:
        if baseline is None:
            raise SpecError("a non_inferiority metric must set baseline", path=path)
        if margin is None:
            raise SpecError("a non_inferiority metric must set margin", path=path)
    elif metric_type in (MetricType.PROPORTION, MetricType.NUMERIC_TOLERANCE, MetricType.MEAN):
        if target is None:
            raise SpecError(
                f"a {metric_type.value} metric must set a target pass rate", path=path
            )

    if method is BoundMethod.WALD_LOWER:
        warnings.append(
            f"{path}.method: the Wald bound is badly behaved at extreme proportions and "
            "claims certainty when every case passes; it should not support a GxP "
            "acceptance claim"
        )
    if method is BoundMethod.NONE:
        warnings.append(
            f"{path}.method: 'none' compares the point estimate to the target with no "
            "confidence bound, which does not support a statistical claim"
        )

    return MetricSpec(
        name=name,
        type=metric_type,
        target=target,
        confidence=confidence if confidence is not None else 0.95,
        method=method,
        scorer=_string(block, "scorer", path) or None,
        tolerance_abs=tolerance_abs,
        tolerance_rel=tolerance_rel,
        max_failures=_integer(block, "max_failures", path, minimum=0),
        max_count=max_count,
        baseline=baseline,
        margin=margin,
        strata=_string_list(block, "strata", path),
        critical=_bool(block, "critical", path, True),
        description=_string(block, "description", path),
    )


def _parse_acceptance(
    data: Mapping[str, Any], strict: bool, warnings: list[str]
) -> AcceptanceSpec:
    path = "acceptance"
    block = _require_mapping(data.get("acceptance"), path)
    if not block:
        raise SpecError(
            "is required: a validation with no acceptance criteria cannot reach a verdict",
            path=path,
        )
    _reject_unknown(block, _ACCEPTANCE_KEYS, path, strict, warnings)

    raw_metrics = block.get("metrics")
    if not raw_metrics:
        raise SpecError("at least one acceptance metric is required", path=f"{path}.metrics")
    if not isinstance(raw_metrics, Sequence) or isinstance(raw_metrics, str):
        raise SpecError("expected a list of metrics", path=f"{path}.metrics")

    metrics = [
        _parse_metric(item, f"{path}.metrics[{index}]", strict, warnings)
        for index, item in enumerate(raw_metrics)
    ]

    seen: dict[str, int] = {}
    for index, metric in enumerate(metrics):
        if metric.name in seen:
            raise SpecError(
                f"duplicate metric name {metric.name!r}; already defined at "
                f"{path}.metrics[{seen[metric.name]}]",
                path=f"{path}.metrics[{index}].name",
            )
        seen[metric.name] = index

    if not any(m.critical for m in metrics):
        raise SpecError(
            "at least one metric must be critical; a specification in which every "
            "criterion is advisory can never fail",
            path=f"{path}.metrics",
        )

    judge_block = block.get("judge_calibration")
    judge = None
    if judge_block is not None:
        judge_path = f"{path}.judge_calibration"
        judge_map = _require_mapping(judge_block, judge_path)
        _reject_unknown(judge_map, _JUDGE_KEYS, judge_path, strict, warnings)
        judge = JudgeCalibrationSpec(
            min_cohen_kappa=_number(
                judge_map, "min_cohen_kappa", judge_path, default=0.80, minimum=-1.0, maximum=1.0
            )
            or 0.0,
            min_percent_agreement=_number(
                judge_map, "min_percent_agreement", judge_path, minimum=0.0, maximum=1.0
            ),
            min_samples=_integer(judge_map, "min_samples", judge_path, default=30, minimum=1)
            or 30,
            required=_bool(judge_map, "required", judge_path, True),
        )

    return AcceptanceSpec(metrics=metrics, judge_calibration=judge)


_MONITORING_KEYS = (
    "schedule",
    "spc_rule",
    "window",
    "alert_channels",
    "auto_change_control",
    "periodic_review_months",
)


def _parse_monitoring(
    data: Mapping[str, Any], strict: bool, warnings: list[str]
) -> MonitoringSpec:
    from ..models import SpcRule

    path = "monitoring"
    block = _require_mapping(data.get("monitoring"), path)
    _reject_unknown(block, _MONITORING_KEYS, path, strict, warnings)

    schedule = _string(block, "schedule", path) or None
    if schedule:
        _validate_cron(schedule, f"{path}.schedule")

    return MonitoringSpec(
        schedule=schedule,
        spc_rule=_enum(
            block.get("spc_rule"), SpcRule, f"{path}.spc_rule", SpcRule.WESTERN_ELECTRIC
        ),
        window=int(_integer(block, "window", path, default=20, minimum=2) or 20),
        alert_channels=_string_list(block, "alert_channels", path),
        auto_change_control=_bool(block, "auto_change_control", path, True),
        periodic_review_months=int(
            _integer(block, "periodic_review_months", path, default=6, minimum=1) or 6
        ),
    )


_SIGNOFF_KEYS = ("approvers", "reviewers", "esignature", "require_distinct_signers")


def _parse_signoff(data: Mapping[str, Any], strict: bool, warnings: list[str]) -> SignoffSpec:
    path = "signoff"
    block = _require_mapping(data.get("signoff"), path)
    _reject_unknown(block, _SIGNOFF_KEYS, path, strict, warnings)

    esignature = _string(block, "esignature", path, default="part11").lower()
    if esignature not in ("part11", "none"):
        raise SpecError(
            f"expected 'part11' or 'none', got {esignature!r}", path=f"{path}.esignature"
        )

    approvers = _string_list(block, "approvers", path)
    if esignature == "part11" and not approvers:
        raise SpecError(
            "at least one approver is required when Part 11 signatures are in force",
            path=f"{path}.approvers",
        )

    return SignoffSpec(
        approvers=approvers,
        reviewers=_string_list(block, "reviewers", path),
        esignature=esignature,
        require_distinct_signers=_bool(block, "require_distinct_signers", path, True),
    )


# --------------------------------------------------------------------------
# Cross-block warnings
# --------------------------------------------------------------------------


def _cross_check(spec: AgentSpec, warnings: list[str]) -> None:
    """Flag combinations that are individually legal but weaken the package."""
    datasets = spec.datasets

    if datasets.golden_set is None:
        warnings.append(
            "datasets.golden_set: no golden set declared; the acceptance battery has "
            "nothing to run against"
        )
    elif not datasets.golden_set.sha256:
        warnings.append(
            "datasets.golden_set.sha256: the golden set is not pinned to a digest, so the "
            "run cannot be proven reproducible and the OQ cannot be repeated exactly. "
            "Pin the digest before executing a qualification run."
        )

    if spec.acceptance.judge_calibration is not None and not spec.models.judge:
        warnings.append(
            "acceptance.judge_calibration: a calibration threshold is set but no judge "
            "model is configured under models.judge; the threshold will never be applied"
        )

    if spec.models.judge and spec.acceptance.judge_calibration is None:
        warnings.append(
            "models.judge: a judge model is configured with no judge_calibration block. "
            "An LLM judge is an unvalidated measuring instrument until its agreement with "
            "human labels is quantified."
        )

    if datasets.red_team is None:
        warnings.append(
            "datasets.red_team: no adversarial set declared; prompt injection and "
            "out-of-scope use will not be exercised"
        )

    if not spec.monitoring.schedule:
        warnings.append(
            "monitoring.schedule: no re-evaluation schedule set. A model provider can "
            "change behaviour without any change on your side, so a validated status with "
            "no monitoring decays silently."
        )

    if spec.context_of_use.patient_safety_impact and spec.context_of_use.human_in_the_loop is False:
        warnings.append(
            "context_of_use: the agent is declared to have patient-safety impact with no "
            "human in the loop. This combination warrants explicit quality review."
        )

    if spec.models.phi_safe_local is None:
        warnings.append(
            "models.phi_safe_local: no local model configured. If the golden set contains "
            "PHI, evaluation will refuse to run rather than send it to a hosted provider."
        )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def parse_spec(text: str, source: str | None = None, *, strict: bool = True) -> SpecLoadResult:
    """Parse and validate a specification from YAML text.

    ``source_sha256`` on the returned spec is the digest of the raw text, not
    of the parsed structure: the validation plan quotes it to identify the
    exact file that was reviewed and approved.
    """
    try:
        loaded = yaml.safe_load(io.StringIO(text))
    except yaml.YAMLError as error:
        raise SpecError(f"could not be parsed as YAML: {error}", path=source or "<spec>") from error

    if loaded is None:
        raise SpecError("is empty", path=source or "<spec>")
    if not isinstance(loaded, Mapping):
        raise SpecError(
            f"must be a mapping at the top level, got {type(loaded).__name__}",
            path=source or "<spec>",
        )

    data = dict(loaded)
    warnings: list[str] = []

    top_level = (
        "apiVersion",
        "kind",
        "metadata",
        "context_of_use",
        "intended_use",
        "gamp",
        "models",
        "datasets",
        "acceptance",
        "monitoring",
        "signoff",
    )
    _reject_unknown(data, top_level, source or "<spec>", strict, warnings)

    api_version = _string(data, "apiVersion", "", default=SUPPORTED_API_VERSIONS[0])
    if api_version not in SUPPORTED_API_VERSIONS:
        raise SpecError(
            f"unsupported apiVersion {api_version!r}; this build supports "
            f"{', '.join(SUPPORTED_API_VERSIONS)}",
            path="apiVersion",
        )
    kind = _string(data, "kind", "", default=SUPPORTED_KINDS[0])
    if kind not in SUPPORTED_KINDS:
        raise SpecError(
            f"unsupported kind {kind!r}; this build supports {', '.join(SUPPORTED_KINDS)}",
            path="kind",
        )

    spec = AgentSpec(
        metadata=_parse_metadata(data, strict, warnings),
        context_of_use=_parse_context_of_use(data, strict, warnings),
        intended_use=_parse_intended_use(data, strict, warnings),
        gamp=_parse_gamp(data, strict, warnings),
        models=_parse_models(data, strict, warnings),
        datasets=_parse_datasets(data, strict, warnings),
        acceptance=_parse_acceptance(data, strict, warnings),
        monitoring=_parse_monitoring(data, strict, warnings),
        signoff=_parse_signoff(data, strict, warnings),
        api_version=api_version,
        kind=kind,
        source_sha256=sha256_text(text),
    )

    _cross_check(spec, warnings)
    return SpecLoadResult(spec=spec, warnings=warnings, source=source or "<string>")


def load_spec_from_string(
    text: str, source: str | None = None, *, strict: bool = True
) -> AgentSpec:
    """Parse a specification from YAML text, discarding warnings."""
    return parse_spec(text, source, strict=strict).spec


def load_spec(path: str | os.PathLike[str], *, strict: bool = True) -> AgentSpec:
    """Load and validate a specification from a file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError as error:
        raise SpecError(f"file not found: {path}", path=str(path)) from error
    except OSError as error:
        raise SpecError(f"could not be read: {error}", path=str(path)) from error
    return parse_spec(text, str(path), strict=strict).spec


def load_spec_result(path: str | os.PathLike[str], *, strict: bool = True) -> SpecLoadResult:
    """Load a specification from a file, keeping the warnings."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    return parse_spec(text, str(path), strict=strict)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _prune(value: Any) -> Any:
    """Drop empty containers and ``None`` so a dumped spec stays readable."""
    if isinstance(value, dict):
        cleaned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def dump_spec(spec: AgentSpec) -> str:
    """Render a specification back to YAML.

    Round-trips through :func:`parse_spec` to the same logical specification,
    which is what lets the pipeline store the effective spec alongside the
    original as evidence of what was actually executed.
    """
    document: dict[str, Any] = {
        "apiVersion": spec.api_version,
        "kind": spec.kind,
        "metadata": {
            "agent_id": spec.metadata.agent_id,
            "version": spec.metadata.version,
            "owner": spec.metadata.owner,
            "system_of_record": spec.metadata.system_of_record,
            "description": spec.metadata.description,
            "labels": dict(spec.metadata.labels),
        },
        "context_of_use": {
            "question_of_interest": spec.context_of_use.question_of_interest,
            "role": spec.context_of_use.role,
            "model_influence": spec.context_of_use.model_influence.value,
            "decision_consequence": spec.context_of_use.decision_consequence.value,
            "regulatory_impact": spec.context_of_use.regulatory_impact.value,
            "human_in_the_loop": spec.context_of_use.human_in_the_loop,
            "patient_safety_impact": spec.context_of_use.patient_safety_impact,
            "product_quality_impact": spec.context_of_use.product_quality_impact,
            "data_integrity_impact": spec.context_of_use.data_integrity_impact,
        },
        "intended_use": {
            "in_scope": list(spec.intended_use.in_scope),
            "out_of_scope": list(spec.intended_use.out_of_scope),
            "users": list(spec.intended_use.users),
            "limitations": list(spec.intended_use.limitations),
        },
        "gamp": {
            "category": int(spec.gamp.category.value),
            "risk_class": spec.gamp.risk_class.value if spec.gamp.risk_class else None,
            "rationale": spec.gamp.rationale,
        },
        "models": {
            "primary": spec.models.primary,
            "judge": spec.models.judge,
            "phi_safe_local": spec.models.phi_safe_local,
            "temperature": spec.models.temperature,
            "seed": spec.models.seed,
            "max_tokens": spec.models.max_tokens,
            "parameters": dict(spec.models.parameters),
        },
        "datasets": _dump_datasets(spec.datasets),
        "acceptance": _dump_acceptance(spec.acceptance),
        "monitoring": {
            "schedule": spec.monitoring.schedule,
            "spc_rule": spec.monitoring.spc_rule.value,
            "window": spec.monitoring.window,
            "alert_channels": list(spec.monitoring.alert_channels),
            "auto_change_control": spec.monitoring.auto_change_control,
            "periodic_review_months": spec.monitoring.periodic_review_months,
        },
        "signoff": {
            "approvers": list(spec.signoff.approvers),
            "reviewers": list(spec.signoff.reviewers),
            "esignature": spec.signoff.esignature,
            "require_distinct_signers": spec.signoff.require_distinct_signers,
        },
    }
    return yaml.safe_dump(_prune(document), sort_keys=False, default_flow_style=False, width=88)


def _dump_dataset(dataset: DatasetSpec | None) -> dict[str, Any] | None:
    if dataset is None:
        return None
    return {
        "ref": dataset.ref,
        "sha256": dataset.sha256,
        "version": dataset.version,
        "description": dataset.description,
    }


def _dump_datasets(datasets: DatasetsSpec) -> dict[str, Any]:
    out: dict[str, Any] = {
        "golden_set": _dump_dataset(datasets.golden_set),
        "red_team": _dump_dataset(datasets.red_team),
        "calibration_set": _dump_dataset(datasets.calibration_set),
    }
    if datasets.additional:
        out["additional"] = {
            name: _dump_dataset(value) for name, value in sorted(datasets.additional.items())
        }
    return out


def _dump_acceptance(acceptance: AcceptanceSpec) -> dict[str, Any]:
    metrics = []
    for metric in acceptance.metrics:
        entry: dict[str, Any] = {
            "name": metric.name,
            "type": metric.type.value,
            "target": metric.target,
            "confidence": metric.confidence,
            "method": metric.method.value,
            "scorer": metric.scorer,
            "tolerance_abs": metric.tolerance_abs,
            "tolerance_rel": metric.tolerance_rel,
            "max_failures": metric.max_failures,
            "max_count": metric.max_count,
            "baseline": metric.baseline,
            "margin": metric.margin,
            "strata": list(metric.strata),
            "description": metric.description,
        }
        # ``critical`` defaults to true; emit it only when it is false, so the
        # common case stays uncluttered but the exception is always explicit.
        if not metric.critical:
            entry["critical"] = False
        metrics.append(entry)

    out: dict[str, Any] = {"metrics": metrics}
    if acceptance.judge_calibration is not None:
        judge = acceptance.judge_calibration
        out["judge_calibration"] = {
            "min_cohen_kappa": judge.min_cohen_kappa,
            "min_percent_agreement": judge.min_percent_agreement,
            "min_samples": judge.min_samples,
            "required": judge.required,
        }
    return out
