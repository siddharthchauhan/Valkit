"""Tests for specification loading and validation.

A silently-accepted mistake in a specification becomes a wrong statement in a
signed document, so most of these tests are about rejection: what the loader
refuses, and whether it says precisely enough where the problem is.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from valkit.errors import SpecError
from valkit.models import BoundMethod, GampCategory, MetricType, RiskLevel
from valkit.spec.loader import dump_spec, load_spec, load_spec_from_string, parse_spec
from valkit.testing import EXAMPLE_YAML
from valkit.util import sha256_text

MINIMAL = textwrap.dedent(
    """
    apiVersion: valkit/v1
    kind: AgentValidation
    metadata:
      agent_id: demo-agent
      version: "1.0.0"
    context_of_use:
      question_of_interest: Does the agent extract the right value?
      role: Assistive; a human reviews every output.
    models:
      primary: fixture/deterministic
    acceptance:
      metrics:
        - name: accuracy
          target: 0.95
    signoff:
      approvers: [qa_lead]
    """
)


BASE = yaml.safe_load(MINIMAL)


def _merge(base: dict, overrides: dict) -> dict:
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def build(**overrides) -> str:
    """Render a valid specification with the given blocks merged in.

    Built structurally rather than by concatenating YAML text: appending a
    second ``context_of_use:`` block would silently replace the first, and a
    test that quietly drops a required field is worse than no test.
    """
    return yaml.safe_dump(_merge(BASE, overrides), sort_keys=False)


def spec_with(block: str) -> str:
    return build(**yaml.safe_load(textwrap.dedent(block) or "{}"))


class TestHappyPath:
    def test_minimal_spec_loads(self):
        spec = load_spec_from_string(MINIMAL)
        assert spec.agent_id == "demo-agent"
        assert spec.version == "1.0.0"
        assert spec.ref == "demo-agent@1.0.0"
        assert len(spec.acceptance.metrics) == 1

    def test_example_from_the_design_document_loads(self):
        result = parse_spec(EXAMPLE_YAML, "example")
        assert result.spec.agent_id == "rave-als-generator"
        assert [m.name for m in result.spec.acceptance.metrics] == [
            "field_accuracy",
            "citation_accuracy",
            "numeric_tolerance",
        ]
        assert result.spec.gamp.category is GampCategory.BESPOKE

    def test_source_digest_is_of_the_raw_text(self):
        """The validation plan quotes this to identify the reviewed file."""
        spec = load_spec_from_string(MINIMAL)
        assert spec.source_sha256 == sha256_text(MINIMAL)

    def test_defaults_are_applied(self):
        spec = load_spec_from_string(MINIMAL)
        assert spec.acceptance.metrics[0].confidence == 0.95
        assert spec.acceptance.metrics[0].method is BoundMethod.CLOPPER_PEARSON_LOWER
        assert spec.context_of_use.human_in_the_loop is True
        assert spec.gamp.category is GampCategory.BESPOKE

    def test_loads_from_a_file(self, tmp_path):
        path = tmp_path / "valkit.yaml"
        path.write_text(MINIMAL, encoding="utf-8")
        assert load_spec(path).agent_id == "demo-agent"

    def test_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(SpecError, match="file not found"):
            load_spec(tmp_path / "absent.yaml")


class TestShorthands:
    def test_dataset_as_a_bare_string(self):
        spec = load_spec_from_string(
            spec_with(
                """
                datasets:
                  golden_set: s3://bucket/golden.jsonl
                """
            )
        )
        assert spec.datasets.golden_set.ref == "s3://bucket/golden.jsonl"
        assert spec.datasets.golden_set.sha256 is None

    def test_dataset_as_a_mapping(self):
        digest = "a" * 64
        spec = load_spec_from_string(
            spec_with(
                f"""
                datasets:
                  golden_set:
                    ref: golden.jsonl
                    sha256: {digest}
                    version: v7
                """
            )
        )
        assert spec.datasets.golden_set.sha256 == digest
        assert spec.datasets.golden_set.version == "v7"

    def test_enum_values_are_case_insensitive(self):
        spec = load_spec_from_string(
            spec_with(
                """
                gamp:
                  category: 5
                  risk_class: HIGH
                """
            )
        )
        assert spec.gamp.category is GampCategory.BESPOKE
        assert spec.gamp.risk_class.value == "high"

    def test_gamp_category_as_a_string(self):
        spec = load_spec_from_string(spec_with("gamp:\n  category: '4'\n"))
        assert spec.gamp.category is GampCategory.CONFIGURED

    def test_single_string_where_a_list_is_expected(self):
        spec = load_spec_from_string(
            spec_with("intended_use:\n  in_scope: just one thing\n")
        )
        assert spec.intended_use.in_scope == ["just one thing"]

    def test_numeric_version_is_coerced(self):
        text = MINIMAL.replace('version: "1.0.0"', "version: 2.3")
        assert load_spec_from_string(text).version == "2.3"


class TestStructuralRejection:
    def test_empty_document(self):
        with pytest.raises(SpecError, match="is empty"):
            load_spec_from_string("")

    def test_not_a_mapping(self):
        with pytest.raises(SpecError, match="must be a mapping"):
            load_spec_from_string("- a\n- b\n")

    def test_malformed_yaml(self):
        with pytest.raises(SpecError, match="could not be parsed as YAML"):
            load_spec_from_string("a: [1, 2\nb: }")

    def test_unsupported_api_version(self):
        text = MINIMAL.replace("valkit/v1", "valkit/v99")
        with pytest.raises(SpecError, match="unsupported apiVersion"):
            load_spec_from_string(text)

    def test_unsupported_kind(self):
        text = MINIMAL.replace("AgentValidation", "SomethingElse")
        with pytest.raises(SpecError, match="unsupported kind"):
            load_spec_from_string(text)

    def test_unknown_key_rejected_in_strict_mode(self):
        with pytest.raises(SpecError, match="unknown key"):
            load_spec_from_string(spec_with("tolarance: 0.1\n"))

    def test_unknown_key_becomes_a_warning_when_not_strict(self):
        result = parse_spec(spec_with("tolarance: 0.1\n"), strict=False)
        assert any("unknown key" in w for w in result.warnings)

    def test_unknown_nested_key_names_its_path(self):
        text = build(
            acceptance={"metrics": [{"name": "accuracy", "target": 0.95, "tolarance_abs": 0.1}]}
        )
        with pytest.raises(SpecError, match=r"acceptance\.metrics\[0\]"):
            load_spec_from_string(text)


class TestMetadataValidation:
    def test_metadata_required(self):
        text = MINIMAL.replace("metadata:\n  agent_id: demo-agent\n  version: \"1.0.0\"\n", "")
        with pytest.raises(SpecError, match="metadata"):
            load_spec_from_string(text)

    def test_agent_id_required(self):
        text = MINIMAL.replace("  agent_id: demo-agent\n", "")
        with pytest.raises(SpecError, match=r"metadata\.agent_id"):
            load_spec_from_string(text)

    @pytest.mark.parametrize("bad", ["Demo-Agent", "a", "-leading", "trailing-", "has space"])
    def test_agent_id_shape_enforced(self, bad):
        text = MINIMAL.replace("agent_id: demo-agent", f"agent_id: {bad!r}")
        with pytest.raises(SpecError, match=r"metadata\.agent_id"):
            load_spec_from_string(text)

    def test_empty_version_rejected(self):
        text = MINIMAL.replace('version: "1.0.0"', 'version: ""')
        with pytest.raises(SpecError, match=r"metadata\.version"):
            load_spec_from_string(text)


class TestContextOfUseValidation:
    def test_context_of_use_required(self):
        text = MINIMAL.replace(
            "context_of_use:\n"
            "  question_of_interest: Does the agent extract the right value?\n"
            "  role: Assistive; a human reviews every output.\n",
            "",
        )
        with pytest.raises(SpecError, match="credibility framework"):
            load_spec_from_string(text)

    def test_question_of_interest_required(self):
        text = MINIMAL.replace(
            "  question_of_interest: Does the agent extract the right value?\n", ""
        )
        with pytest.raises(SpecError, match=r"context_of_use\.question_of_interest"):
            load_spec_from_string(text)

    def test_bad_enum_lists_the_options(self):
        with pytest.raises(SpecError) as excinfo:
            load_spec_from_string(spec_with("context_of_use:\n  model_influence: extreme\n"))
        message = str(excinfo.value)
        assert "expected one of" in message
        assert "'low'" in message and "'high'" in message

    def test_non_boolean_flag_rejected(self):
        with pytest.raises(SpecError, match="true or false"):
            load_spec_from_string(spec_with("context_of_use:\n  human_in_the_loop: yes please\n"))


class TestMetricValidation:
    def test_at_least_one_metric_required(self):
        text = MINIMAL.replace(
            "  metrics:\n    - name: accuracy\n      target: 0.95\n", "  metrics: []\n"
        )
        with pytest.raises(SpecError, match="at least one acceptance metric"):
            load_spec_from_string(text)

    def test_duplicate_metric_names_rejected(self):
        text = MINIMAL.replace(
            "    - name: accuracy\n      target: 0.95\n",
            "    - name: accuracy\n      target: 0.95\n    - name: accuracy\n      target: 0.9\n",
        )
        with pytest.raises(SpecError, match="duplicate metric name"):
            load_spec_from_string(text)

    @pytest.mark.parametrize("target", ["0", "0.0", "-0.1", "1.5"])
    def test_target_out_of_range_rejected(self, target):
        text = MINIMAL.replace("target: 0.95", f"target: {target}")
        with pytest.raises(SpecError, match=r"acceptance\.metrics\[0\]"):
            load_spec_from_string(text)

    def test_target_of_one_is_rejected_with_an_explanation(self):
        text = MINIMAL.replace("target: 0.95", "target: 1.0")
        with pytest.raises(SpecError, match="no finite sample can demonstrate"):
            load_spec_from_string(text)

    @pytest.mark.parametrize("confidence", ["0", "1", "1.2", "-0.1"])
    def test_confidence_must_be_strictly_inside_the_unit_interval(self, confidence):
        text = MINIMAL.replace(
            "      target: 0.95", f"      target: 0.95\n      confidence: {confidence}"
        )
        with pytest.raises(SpecError, match="confidence"):
            load_spec_from_string(text)

    def test_numeric_tolerance_metric_needs_a_tolerance(self):
        text = MINIMAL.replace(
            "      target: 0.95", "      target: 0.95\n      type: numeric_tolerance"
        )
        with pytest.raises(SpecError, match="tolerance_abs or tolerance_rel"):
            load_spec_from_string(text)

    def test_count_metric_needs_a_maximum(self):
        text = MINIMAL.replace(
            "    - name: accuracy\n      target: 0.95\n",
            "    - name: defects\n      type: count\n",
        )
        with pytest.raises(SpecError, match="must set max_count"):
            load_spec_from_string(text)

    def test_non_inferiority_needs_baseline_and_margin(self):
        text = MINIMAL.replace(
            "      target: 0.95", "      target: 0.95\n      method: non_inferiority"
        )
        with pytest.raises(SpecError, match="must set baseline"):
            load_spec_from_string(text)

    def test_proportion_metric_needs_a_target(self):
        text = MINIMAL.replace("      target: 0.95\n", "")
        with pytest.raises(SpecError, match="must set a target pass rate"):
            load_spec_from_string(text)

    def test_negative_tolerance_rejected(self):
        text = MINIMAL.replace(
            "      target: 0.95",
            "      target: 0.95\n      type: numeric_tolerance\n      tolerance_abs: -1",
        )
        with pytest.raises(SpecError, match="tolerance_abs"):
            load_spec_from_string(text)

    def test_all_metrics_advisory_is_rejected(self):
        text = MINIMAL.replace("      target: 0.95", "      target: 0.95\n      critical: false")
        with pytest.raises(SpecError, match="at least one metric must be critical"):
            load_spec_from_string(text)


class TestCronValidation:
    @pytest.mark.parametrize("cron", ["0 6 * *", "0 6 * * 1 5", "@weekly"])
    def test_bad_cron_rejected(self, cron):
        with pytest.raises(SpecError, match=r"monitoring\.schedule"):
            load_spec_from_string(spec_with(f"monitoring:\n  schedule: '{cron}'\n"))

    def test_valid_cron_accepted(self):
        spec = load_spec_from_string(spec_with("monitoring:\n  schedule: '0 6 * * 1'\n"))
        assert spec.monitoring.schedule == "0 6 * * 1"


class TestSignoffValidation:
    def test_part11_requires_an_approver(self):
        text = MINIMAL.replace("signoff:\n  approvers: [qa_lead]\n", "signoff:\n  approvers: []\n")
        with pytest.raises(SpecError, match="at least one approver"):
            load_spec_from_string(text)

    def test_unknown_esignature_scheme_rejected(self):
        with pytest.raises(SpecError, match="part11"):
            load_spec_from_string(spec_with("signoff:\n  esignature: docusign\n"))


class TestDatasetDigestValidation:
    def test_short_digest_rejected(self):
        with pytest.raises(SpecError, match="64-character hex"):
            load_spec_from_string(
                spec_with("datasets:\n  golden_set:\n    ref: g.jsonl\n    sha256: abc123\n")
            )

    def test_dataset_ref_required_in_mapping_form(self):
        with pytest.raises(SpecError, match=r"datasets\.golden_set\.ref"):
            load_spec_from_string(spec_with("datasets:\n  golden_set:\n    version: v1\n"))


class TestWarnings:
    def test_unpinned_golden_set_warns_about_reproducibility(self):
        result = parse_spec(spec_with("datasets:\n  golden_set: g.jsonl\n"))
        assert any("not pinned to a digest" in w for w in result.warnings)

    def test_judge_without_calibration_warns(self):
        result = parse_spec(spec_with("models:\n  primary: x\n  judge: y\n"))
        assert any("unvalidated measuring instrument" in w for w in result.warnings)

    def test_calibration_without_judge_warns(self):
        result = parse_spec(
            build(
                acceptance={
                    "metrics": [{"name": "accuracy", "target": 0.95}],
                    "judge_calibration": {"min_cohen_kappa": 0.8},
                }
            )
        )
        assert any("no judge model is configured" in w for w in result.warnings)

    def test_no_monitoring_schedule_warns(self):
        result = parse_spec(MINIMAL)
        assert any("decays silently" in w for w in result.warnings)

    def test_no_red_team_warns(self):
        assert any("no adversarial set" in w for w in parse_spec(MINIMAL).warnings)

    def test_nonzero_temperature_warns_about_reproducibility(self):
        result = parse_spec(spec_with("models:\n  primary: x\n  temperature: 0.7\n"))
        assert any("non-reproducible" in w for w in result.warnings)

    def test_wald_method_warns(self):
        text = MINIMAL.replace("      target: 0.95", "      target: 0.95\n      method: wald_lower")
        assert any("should not support a GxP" in w for w in parse_spec(text).warnings)

    def test_a_fully_specified_spec_has_no_reproducibility_warning(self):
        result = parse_spec(
            spec_with(
                f"""
                datasets:
                  golden_set:
                    ref: g.jsonl
                    sha256: {'b' * 64}
                  red_team: r.jsonl
                monitoring:
                  schedule: '0 6 * * 1'
                models:
                  primary: fixture/deterministic
                  phi_safe_local: ollama/local
                """
            )
        )
        assert not any("not pinned" in w for w in result.warnings)
        assert not any("decays silently" in w for w in result.warnings)


class TestRoundTrip:
    def test_dump_reloads_to_the_same_specification(self):
        original = parse_spec(EXAMPLE_YAML, "example").spec
        reloaded = load_spec_from_string(dump_spec(original))
        # The source digest necessarily differs, since the text differs.
        assert reloaded.replace(source_sha256=None) == original.replace(source_sha256=None)

    def test_dump_is_stable(self):
        spec = parse_spec(EXAMPLE_YAML, "example").spec
        once = dump_spec(spec)
        twice = dump_spec(load_spec_from_string(once))
        assert once == twice

    def test_dump_is_valid_yaml(self):
        text = dump_spec(parse_spec(EXAMPLE_YAML, "example").spec)
        assert isinstance(yaml.safe_load(text), dict)

    def test_dump_omits_empty_blocks(self):
        text = dump_spec(load_spec_from_string(MINIMAL))
        assert "additional" not in text
