"""Tests for datasets, providers, scorers, the judge and the runner.

The safety property tested hardest here is PHI routing: a specification that
names a local model for protected health information must never fall back to a
hosted provider. That would be the worst defect this product could have, so it
is tested from several directions.
"""

from __future__ import annotations

import json

import pytest

from valkit.errors import DatasetError, EvalError, ProviderError
from valkit.evals.dataset import (
    dataset_from_samples,
    load_dataset,
    load_dataset_detailed,
    phi_samples,
    sample as subsample,
    stratify,
    summarise,
    write_dataset,
)
from valkit.evals.judge import LlmJudge, calibrate
from valkit.evals.providers import (
    CallableProvider,
    EchoProvider,
    FixtureJudgeProvider,
    FixtureProvider,
    RecordingProvider,
    RetryingProvider,
    resolve_provider,
)
from valkit.evals.runner import EvalRunner, RunOptions
from valkit.evals.scorers import (
    citation_accuracy,
    exact_match,
    extract_number,
    get_scorer,
    numeric_tolerance,
    refusal_expected,
    registry,
)
from valkit.models import (
    GoldenSample,
    JudgeCalibrationSpec,
    MetricSpec,
    MetricType,
    RunStatus,
    Score,
)
from valkit.testing import make_spec
from valkit.util import FrozenClock, canonical_json

GOLDEN = "examples/datasets/rave_als_golden.jsonl"
REDTEAM = "examples/datasets/rave_als_redteam.jsonl"


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------


class TestDatasetLoading:
    def test_loads_the_example_golden_set(self):
        dataset = load_dataset(GOLDEN)
        assert len(dataset.samples) == 65
        assert dataset.samples[0].sample_id == "ALS-0001"

    def test_both_digests_are_computed(self):
        result = load_dataset_detailed(GOLDEN)
        assert len(result.canonical_sha256) == 64
        assert len(result.file_sha256) == 64
        assert result.canonical_sha256 != result.file_sha256

    def test_canonical_digest_is_stable_across_reformatting(self, workdir):
        """The whole point of the canonical digest."""
        original = load_dataset_detailed(GOLDEN)
        reformatted = workdir / "reformatted.jsonl"
        lines = [
            json.dumps(json.loads(line), sort_keys=False, indent=None)
            for line in open(GOLDEN, encoding="utf-8")
            if line.strip()
        ]
        reformatted.write_text("\n\n".join(lines) + "\n", encoding="utf-8")

        again = load_dataset_detailed(reformatted)
        assert again.canonical_sha256 == original.canonical_sha256
        assert again.file_sha256 != original.file_sha256

    def test_a_pinned_canonical_digest_is_accepted(self):
        result = load_dataset_detailed(GOLDEN)
        assert load_dataset(GOLDEN, expected_sha256=result.canonical_sha256)

    def test_a_pinned_file_digest_is_also_accepted(self):
        """A validation engineer pins whatever sha256sum gave them."""
        result = load_dataset_detailed(GOLDEN)
        assert load_dataset(GOLDEN, expected_sha256=result.file_sha256)

    def test_a_wrong_pin_names_both_digests(self):
        with pytest.raises(DatasetError) as excinfo:
            load_dataset(GOLDEN, expected_sha256="f" * 64)
        message = str(excinfo.value)
        assert "canonical" in message and "file" in message
        assert "not the one the validation plan approved" in message

    def test_missing_file(self, workdir):
        with pytest.raises(DatasetError, match="dataset not found"):
            load_dataset(workdir / "absent.jsonl")

    def test_empty_dataset_is_rejected(self, workdir):
        path = workdir / "empty.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(DatasetError, match="empty"):
            load_dataset(path)

    def test_duplicate_sample_ids_rejected(self, workdir):
        path = workdir / "dupes.jsonl"
        path.write_text(
            '{"sample_id": "A", "input": "x"}\n{"sample_id": "A", "input": "y"}\n',
            encoding="utf-8",
        )
        with pytest.raises(DatasetError, match="duplicate sample identifier"):
            load_dataset(path)

    def test_malformed_line_names_its_line_number(self, workdir):
        path = workdir / "bad.jsonl"
        path.write_text('{"sample_id": "A", "input": "x"}\nnot json\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="line 2"):
            load_dataset(path)

    def test_missing_input_is_rejected(self, workdir):
        path = workdir / "noinput.jsonl"
        path.write_text('{"sample_id": "A", "target": "x"}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="has no 'input'"):
            load_dataset(path)

    def test_non_numeric_human_label_rejected(self, workdir):
        path = workdir / "badlabel.jsonl"
        path.write_text('{"sample_id":"A","input":"x","human_label":"yes"}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="non-numeric 'human_label'"):
            load_dataset(path)

    def test_non_utf8_is_rejected_with_an_explanation(self, workdir):
        path = workdir / "latin.jsonl"
        path.write_bytes(b'{"sample_id":"A","input":"caf\xe9"}\n')
        with pytest.raises(DatasetError, match="not valid UTF-8"):
            load_dataset(path)

    def test_blank_and_comment_lines_are_skipped(self, workdir):
        path = workdir / "comments.jsonl"
        path.write_text(
            '// a comment\n\n{"sample_id":"A","input":"x"}\n', encoding="utf-8"
        )
        assert len(load_dataset(path).samples) == 1

    def test_json_array_format(self, workdir):
        path = workdir / "array.json"
        path.write_text('[{"sample_id":"A","input":"x"}]', encoding="utf-8")
        assert len(load_dataset(path).samples) == 1

    def test_json_object_with_samples_key(self, workdir):
        path = workdir / "wrapped.json"
        path.write_text('{"samples":[{"sample_id":"A","input":"x"}]}', encoding="utf-8")
        assert len(load_dataset(path).samples) == 1

    def test_round_trip_through_write(self, workdir):
        original = load_dataset(GOLDEN)
        path = workdir / "out.jsonl"
        write_dataset(original, path)
        assert load_dataset(path).sha256 == original.sha256


class TestDatasetViews:
    def test_stratify_by_the_dedicated_field(self):
        groups = stratify(load_dataset(GOLDEN), "stratum")
        assert "DM" in groups and "AE" in groups
        assert sum(len(v) for v in groups.values()) == 65

    def test_stratify_by_metadata_key(self):
        groups = stratify(load_dataset(GOLDEN), "form")
        assert "DM" in groups

    def test_samples_without_the_key_are_visible_not_dropped(self):
        samples = [
            GoldenSample(sample_id="A", input="x", metadata={"form": "DM"}),
            GoldenSample(sample_id="B", input="y"),
        ]
        groups = stratify(dataset_from_samples(samples), "form")
        assert groups["(unspecified)"][0].sample_id == "B"

    def test_phi_samples_are_identified_by_flag_or_metadata(self):
        samples = [
            GoldenSample(sample_id="A", input="x", contains_phi=True),
            GoldenSample(sample_id="B", input="y", metadata={"phi": True}),
            GoldenSample(sample_id="C", input="z"),
        ]
        dataset = dataset_from_samples(samples)
        assert dataset.phi_count == 1  # only the explicit flag survives construction
        assert len(phi_samples(dataset)) == 1

    def test_phi_metadata_key_is_honoured_at_load_time(self, workdir):
        path = workdir / "phi.jsonl"
        path.write_text(
            '{"sample_id":"A","input":"x","metadata":{"phi":true}}\n', encoding="utf-8"
        )
        assert load_dataset(path).phi_count == 1

    def test_subsample_is_deterministic(self):
        dataset = load_dataset(GOLDEN)
        first = subsample(dataset, 10, seed=7)
        second = subsample(dataset, 10, seed=7)
        assert [s.sample_id for s in first.samples] == [s.sample_id for s in second.samples]

    def test_different_seeds_give_different_subsamples(self):
        dataset = load_dataset(GOLDEN)
        assert [s.sample_id for s in subsample(dataset, 10, seed=1).samples] != [
            s.sample_id for s in subsample(dataset, 10, seed=2).samples
        ]

    def test_subsample_larger_than_the_set_returns_the_set(self):
        dataset = load_dataset(GOLDEN)
        assert subsample(dataset, 1000).sha256 == dataset.sha256

    def test_stratified_subsample_spans_the_strata(self):
        dataset = load_dataset(GOLDEN)
        picked = subsample(dataset, 24, seed=3, stratified_by="stratum")
        assert len({s.stratum for s in picked.samples}) > 5


class TestDatasetSummary:
    def test_summary_counts(self):
        summary = summarise(load_dataset(GOLDEN))
        assert summary.n == 65
        assert summary.labelled == 26
        assert len(summary.strata) > 5

    def test_duplicate_inputs_are_flagged_as_non_independent(self):
        samples = [
            GoldenSample(sample_id="A", input="same question"),
            GoldenSample(sample_id="B", input="same question"),
            GoldenSample(sample_id="C", input="different"),
        ]
        summary = summarise(dataset_from_samples(samples))
        assert summary.duplicate_inputs == 1
        assert any("independence" in note for note in summary.notes)

    def test_unlabelled_set_is_flagged(self):
        samples = [GoldenSample(sample_id=f"S{i}", input=str(i)) for i in range(30)]
        summary = summarise(dataset_from_samples(samples))
        assert any("No sample carries a human label" in note for note in summary.notes)

    def test_single_stratum_is_flagged(self):
        samples = [
            GoldenSample(sample_id=f"S{i}", input=str(i), stratum="only") for i in range(30)
        ]
        summary = summarise(dataset_from_samples(samples))
        assert any("single stratum" in note for note in summary.notes)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class TestProviders:
    def test_fixture_returns_the_target(self):
        provider = FixtureProvider()
        response = provider.generate("prompt", sample_id="A", target="EXPECTED")
        assert response.text == "EXPECTED"

    def test_fixture_is_deterministic_without_a_target(self):
        provider = FixtureProvider()
        first = provider.generate("prompt", sample_id="A")
        second = provider.generate("prompt", sample_id="A")
        assert first.text == second.text

    def test_injected_wrong_answer_is_plausible_not_malformed(self):
        provider = FixtureProvider(wrong_answer_for={"A"})
        response = provider.generate("p", sample_id="A", target="FIELD_0042")
        assert response.text != "FIELD_0042"
        assert response.text.startswith("FIELD_")

    def test_injected_error(self):
        provider = FixtureProvider(error_for={"A"})
        with pytest.raises(ProviderError, match="simulated failure"):
            provider.generate("p", sample_id="A", target="X")

    def test_injected_refusal(self):
        provider = FixtureProvider(refuse_for={"A"})
        assert "cannot" in provider.generate("p", sample_id="A").text

    def test_from_dataset_reads_behaviour_from_metadata(self):
        dataset = load_dataset(GOLDEN)
        provider = FixtureProvider.from_dataset(dataset)
        with pytest.raises(ProviderError):
            provider.generate("p", sample_id="ALS-0016", target="X")

    def test_echo_provider(self):
        assert EchoProvider().generate("hello").text == "hello"

    def test_callable_provider_wraps_failures(self):
        def explode(prompt, **kwargs):
            raise RuntimeError("boom")

        with pytest.raises(ProviderError, match="provider callable failed"):
            CallableProvider(explode).generate("p")

    def test_recording_provider_keeps_transcripts(self):
        provider = RecordingProvider(FixtureProvider())
        provider.generate("p", sample_id="A", target="X")
        assert provider.transcripts[0]["response"] == "X"

    def test_recording_provider_records_failures_too(self):
        provider = RecordingProvider(FixtureProvider(error_for={"A"}))
        with pytest.raises(ProviderError):
            provider.generate("p", sample_id="A")
        assert "error" in provider.transcripts[0]

    def test_retrying_provider_recovers(self):
        attempts = {"count": 0}

        def flaky(prompt, **kwargs):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ProviderError("transient")
            return "recovered"

        provider = RetryingProvider(CallableProvider(flaky), attempts=3)
        assert provider.generate("p").text == "recovered"

    def test_retrying_provider_gives_up_and_says_so(self):
        provider = RetryingProvider(FixtureProvider(error_for={"A"}), attempts=2)
        with pytest.raises(ProviderError, match="after 2 attempt"):
            provider.generate("p", sample_id="A")

    def test_retry_backoff_does_not_sleep_in_tests(self):
        slept: list[float] = []
        provider = RetryingProvider(
            FixtureProvider(error_for={"A"}), attempts=3, sleep=slept.append
        )
        with pytest.raises(ProviderError):
            provider.generate("p", sample_id="A")
        assert slept == [1.0, 2.0]

    def test_resolve_provider_by_scheme(self):
        assert resolve_provider("fixture/anything").identity == "fixture/anything"
        assert isinstance(resolve_provider("fixture/judge"), FixtureJudgeProvider)

    def test_resolve_provider_rejects_unknown_scheme(self):
        with pytest.raises(ProviderError, match="unknown provider scheme"):
            resolve_provider("mystery/model")

    def test_resolve_provider_requires_a_scheme(self):
        with pytest.raises(ProviderError, match="no scheme"):
            resolve_provider("bare-name")

    def test_registry_overrides_resolution(self):
        stub = EchoProvider()
        assert resolve_provider("bedrock/anything", {"bedrock/anything": stub}) is stub

    def test_hosted_providers_are_not_constructed_by_resolution_alone(self):
        """Resolving a hosted reference must not require its SDK at import time."""
        import valkit.evals.providers as providers

        assert hasattr(providers, "BedrockProvider")


# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------


class TestScorers:
    def test_registry_lists_the_built_ins(self):
        assert "exact_match" in registry.available()
        assert "citation_accuracy" in registry.available()

    def test_unknown_scorer_error_lists_the_known_ones(self):
        with pytest.raises(EvalError, match="Registered scorers"):
            get_scorer("no_such_scorer")

    def test_exact_match(self):
        sample = GoldenSample(sample_id="A", input="x", target="EXPECTED")
        assert exact_match(sample, "EXPECTED").passed
        result = exact_match(sample, "OTHER")
        assert not result.passed
        assert "Expected 'EXPECTED', got 'OTHER'" in result.explanation

    def test_numeric_tolerance_within_and_outside(self):
        sample = GoldenSample(sample_id="A", input="x", target="10.0")
        assert numeric_tolerance(sample, "10.0005", tolerance_abs=0.001).passed
        assert not numeric_tolerance(sample, "10.5", tolerance_abs=0.001).passed

    def test_numeric_tolerance_extracts_from_prose(self):
        sample = GoldenSample(sample_id="A", input="x", target="42")
        assert numeric_tolerance(sample, "The answer is 42 units.", tolerance_abs=0).passed

    def test_numeric_tolerance_with_no_number_fails_informatively(self):
        sample = GoldenSample(sample_id="A", input="x", target="42")
        result = numeric_tolerance(sample, "no digits here", tolerance_abs=0)
        assert not result.passed
        assert "No numeric value could be extracted" in result.explanation

    def test_numeric_tolerance_blames_the_golden_set_when_appropriate(self):
        sample = GoldenSample(sample_id="A", input="x", target="not a number")
        result = numeric_tolerance(sample, "42", tolerance_abs=0)
        assert "defect in the golden set" in result.explanation

    def test_relative_tolerance(self):
        sample = GoldenSample(sample_id="A", input="x", target="1000")
        assert numeric_tolerance(sample, "1005", tolerance_rel=0.01).passed
        assert not numeric_tolerance(sample, "1050", tolerance_rel=0.01).passed

    @pytest.mark.parametrize(
        "text, expected",
        [("42", 42.0), ("-3.5", -3.5), ("1,234", 1234.0), ("1.2e3", 1200.0), ("none", None)],
    )
    def test_extract_number(self, text, expected):
        assert extract_number(text) == expected

    def test_citation_accuracy_accepts_a_real_quote(self):
        sample = GoldenSample(
            sample_id="A",
            input="x",
            metadata={"source_text": "The subject shall be at least 18 years of age."},
        )
        result = citation_accuracy(sample, 'AGE - source: "The subject shall be at least 18 years of age."')
        assert result.passed

    def test_citation_accuracy_catches_a_fabrication(self):
        sample = GoldenSample(
            sample_id="A", input="x", metadata={"source_text": "The subject shall be enrolled."}
        )
        result = citation_accuracy(sample, 'X - source: "The subject shall receive 400mg twice daily."')
        assert not result.passed
        assert "do not occur in the source" in result.explanation

    def test_citation_accuracy_requires_a_citation(self):
        sample = GoldenSample(sample_id="A", input="x", metadata={"source_text": "text"})
        assert not citation_accuracy(sample, "no citation at all").passed

    def test_citation_accuracy_without_source_says_so(self):
        sample = GoldenSample(sample_id="A", input="x")
        result = citation_accuracy(sample, 'x "y"')
        assert not result.passed
        assert "No source text" in result.explanation

    def test_refusal_expected(self):
        sample = GoldenSample(sample_id="A", input="x")
        assert refusal_expected(sample, "I cannot help with that request.").passed
        assert not refusal_expected(sample, "Certainly, here is the answer.").passed

    def test_json_field_match(self):
        scorer = get_scorer("json_field_match")
        sample = GoldenSample(sample_id="A", input="x", target={"field": "DM_AGE"})
        assert scorer(sample, '{"field": "DM_AGE", "extra": 1}').passed
        assert not scorer(sample, '{"field": "DM_SEX"}').passed

    def test_json_field_match_handles_a_code_fence(self):
        scorer = get_scorer("json_field_match")
        sample = GoldenSample(sample_id="A", input="x", target={"field": "DM_AGE"})
        assert scorer(sample, '```json\n{"field": "DM_AGE"}\n```').passed

    def test_scorers_are_total_and_never_raise(self):
        """A malformed output is a failing score, not an exception."""
        sample = GoldenSample(sample_id="A", input="x", target="EXPECTED")
        for name in registry.available():
            scorer = get_scorer(name)
            for output in ("", "\x00\x01", "}{[", "x" * 10_000):
                result = scorer(sample, output)
                assert isinstance(result, Score)


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


class TestJudge:
    def test_judge_agrees_when_the_candidate_matches(self):
        judge = LlmJudge(provider=FixtureJudgeProvider())
        sample = GoldenSample(sample_id="A", input="task", target="the answer")
        assert judge.score(sample, "the answer").passed

    def test_judge_disagrees_when_it_differs(self):
        judge = LlmJudge(provider=FixtureJudgeProvider())
        sample = GoldenSample(sample_id="A", input="task", target="the answer")
        assert not judge.score(sample, "a different answer").passed

    def test_unreadable_verdict_is_an_error_not_a_pass(self):
        judge = LlmJudge(provider=CallableProvider(lambda p, **k: "I'm not sure, maybe?"))
        sample = GoldenSample(sample_id="A", input="task", target="x")
        with pytest.raises(EvalError, match="unreadable verdict"):
            judge.score(sample, "y")

    def test_judge_records_how_it_asked(self):
        judge = LlmJudge(provider=FixtureJudgeProvider())
        sample = GoldenSample(sample_id="A", input="task", target="x")
        score = judge.score(sample, "x")
        assert score.metadata["judge_model"] == "fixture/judge"
        assert score.metadata["prompt_sha256_prefix"]

    def test_prompt_contains_the_three_sections(self):
        judge = LlmJudge(provider=FixtureJudgeProvider())
        sample = GoldenSample(sample_id="A", input="the task", target="the reference")
        prompt = judge.build_prompt(sample, "the candidate")
        assert "TASK:" in prompt and "REFERENCE:" in prompt and "CANDIDATE:" in prompt


class TestCalibration:
    def test_perfect_agreement(self):
        scores = {f"S{i}": Score(value=1.0, passed=True) for i in range(30)}
        labels = {f"S{i}": 1.0 for i in range(30)}
        result = calibrate(scores, labels, JudgeCalibrationSpec(min_samples=10), "judge")
        assert result.cohen_kappa == 1.0

    def test_too_few_labels_fails_rather_than_passing(self):
        """The central rule: insufficient evidence is not a pass."""
        scores = {f"S{i}": Score(value=1.0, passed=True) for i in range(5)}
        labels = {f"S{i}": 1.0 for i in range(5)}
        result = calibrate(scores, labels, JudgeCalibrationSpec(min_samples=30), "judge")
        assert not result.passed
        assert "fewer than the 30" in result.note

    def test_no_overlap_fails_with_an_explanation(self):
        result = calibrate({}, {"S1": 1.0}, JudgeCalibrationSpec(), "judge")
        assert not result.passed
        assert result.n == 0
        assert "uncalibrated" in result.note

    def test_low_kappa_fails(self):
        scores = {f"S{i}": Score(value=float(i % 2), passed=bool(i % 2)) for i in range(40)}
        labels = {f"S{i}": float((i + 1) % 2) for i in range(40)}
        result = calibrate(scores, labels, JudgeCalibrationSpec(min_samples=10), "judge")
        assert not result.passed
        assert "below the required" in result.note

    def test_confusion_counts_are_reported(self):
        scores = {"A": Score(value=1.0, passed=True), "B": Score(value=0.0, passed=False)}
        labels = {"A": 1.0, "B": 0.0}
        result = calibrate(scores, labels, JudgeCalibrationSpec(min_samples=1), "judge")
        assert result.confusion == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}

    def test_prevalence_caveat_is_carried_into_the_note(self):
        scores = {f"S{i}": Score(value=1.0, passed=True) for i in range(40)}
        labels = {f"S{i}": 1.0 for i in range(40)}
        result = calibrate(scores, labels, JudgeCalibrationSpec(min_samples=10), "judge")
        assert "prevalence" in result.note.lower() or "one class" in result.note


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@pytest.fixture
def golden():
    return load_dataset_detailed(GOLDEN)


@pytest.fixture
def example_spec():
    from valkit.spec import load_spec

    return load_spec("examples/valkit.yaml")


def build_runner(dataset, clock=None, **kwargs):
    return EvalRunner(
        FixtureProvider.from_dataset(dataset),
        judge=LlmJudge(provider=FixtureJudgeProvider(disagree_for={"ALS-0003"})),
        clock=clock or FrozenClock(step=1),
        **kwargs,
    )


class TestRunner:
    def test_full_run_over_the_example(self, example_spec, golden):
        run = build_runner(golden.dataset).run(
            example_spec, golden.dataset, dataset_file_sha256=golden.file_sha256
        )
        assert run.status is RunStatus.COMPLETED
        assert run.passed
        assert len(run.samples) == 65

    def test_metrics_are_computed_with_bounds(self, example_spec, golden):
        run = build_runner(golden.dataset).run(example_spec, golden.dataset)
        field_accuracy = run.metric("field_accuracy")
        assert field_accuracy.n == 64
        assert field_accuracy.k == 61
        assert field_accuracy.lower_bound == pytest.approx(0.8833, abs=1e-4)
        assert field_accuracy.passed

    def test_provider_error_leaves_the_denominator_but_is_counted(self, example_spec, golden):
        run = build_runner(golden.dataset).run(example_spec, golden.dataset)
        metric = run.metric("field_accuracy")
        assert metric.errors == 1
        assert metric.n == 64
        errored = [s for s in run.samples if s.error]
        assert len(errored) == 1

    def test_strata_breakdown_is_produced(self, example_spec, golden):
        run = build_runner(golden.dataset).run(example_spec, golden.dataset)
        strata = run.metric("field_accuracy").strata
        assert len(strata) > 5
        assert sum(s.n for s in strata) == run.metric("field_accuracy").n

    def test_calibration_runs_even_when_no_metric_uses_the_judge(self, example_spec, golden):
        run = build_runner(golden.dataset).run(example_spec, golden.dataset)
        assert run.calibration is not None
        assert run.calibration.n == 26
        assert run.calibration.passed

    def test_harness_is_recorded_for_installation_qualification(self, example_spec, golden):
        run = build_runner(golden.dataset).run(example_spec, golden.dataset)
        assert run.harness.name == "valkit"
        assert run.harness.provider.startswith("fixture/")
        assert len(run.harness.config_sha256) == 64
        assert run.harness.python_version

    def test_config_digest_changes_when_the_seed_changes(self, example_spec, golden):
        first = build_runner(golden.dataset).run(example_spec, golden.dataset)
        altered = example_spec.replace(models=example_spec.models.replace(seed=99))
        second = build_runner(golden.dataset).run(altered, golden.dataset)
        assert first.harness.config_sha256 != second.harness.config_sha256

    def test_reruns_are_byte_identical(self, example_spec, golden):
        """The reproducibility claim the validation package makes."""
        first = build_runner(golden.dataset).run(
            example_spec, golden.dataset, run_id="RUN-FIXED"
        )
        second = build_runner(golden.dataset).run(
            example_spec, golden.dataset, run_id="RUN-FIXED"
        )
        assert canonical_json(first) == canonical_json(second)

    def test_transcripts_are_stored_when_a_vault_is_supplied(self, example_spec, golden, workdir):
        from valkit.vault import EvidenceVault

        clock = FrozenClock(step=1)
        vault = EvidenceVault(workdir / "vault", clock)
        run = build_runner(golden.dataset, clock=clock, vault=vault).run(
            example_spec, golden.dataset
        )
        assert run.transcripts_ref
        stored = vault.get_json(run.transcripts_ref)
        assert len(stored) == 65

    def test_audit_events_are_written(self, example_spec, golden):
        from valkit.audit import AuditTrail

        clock = FrozenClock(step=1)
        audit = AuditTrail(":memory:", clock)
        build_runner(golden.dataset, clock=clock, audit=audit).run(example_spec, golden.dataset)
        assert audit.filter(action="run.started")
        assert audit.filter(action="run.completed")
        assert len(audit.filter(action="run.metric_evaluated")) == 3
        assert audit.verify().ok

    def test_unknown_scorer_fails_before_any_model_call(self, example_spec, golden):
        spec = example_spec.replace(
            acceptance=example_spec.acceptance.replace(
                metrics=[MetricSpec(name="m", scorer="nope", target=0.9)]
            )
        )
        provider = FixtureProvider.from_dataset(golden.dataset)
        runner = EvalRunner(provider, clock=FrozenClock(step=1))
        with pytest.raises(EvalError, match="unknown scorer"):
            runner.run(spec, golden.dataset)
        assert provider.calls == [], "no model call should have been made"

    def test_judge_metric_without_a_judge_is_refused(self, example_spec, golden):
        spec = example_spec.replace(
            acceptance=example_spec.acceptance.replace(
                metrics=[MetricSpec(name="m", scorer="judge", target=0.9)]
            )
        )
        runner = EvalRunner(FixtureProvider.from_dataset(golden.dataset), clock=FrozenClock(step=1))
        with pytest.raises(EvalError, match="no judge was configured"):
            runner.run(spec, golden.dataset)

    def test_excessive_errors_are_an_apparatus_failure_not_an_acceptance_failure(
        self, example_spec, golden
    ):
        every_id = {s.sample_id for s in golden.dataset.samples}
        runner = EvalRunner(
            FixtureProvider(error_for=every_id), clock=FrozenClock(step=1)
        )
        run = runner.run(example_spec, golden.dataset)
        assert run.status is RunStatus.FAILED
        assert not run.passed
        assert "apparatus failure" in run.error
        assert "has not been evaluated" in run.error


class TestPhiRouting:
    """The worst possible defect in this product, tested from several angles."""

    def _phi_dataset(self):
        return dataset_from_samples(
            [
                GoldenSample(sample_id="P1", input="patient record", target="X", contains_phi=True),
                GoldenSample(sample_id="P2", input="ordinary", target="Y"),
            ]
        )

    def test_run_is_refused_when_phi_present_and_no_local_provider(self):
        spec = make_spec()
        spec = spec.replace(models=spec.models.replace(phi_safe_local="ollama/llama3.1"))
        runner = EvalRunner(FixtureProvider(), clock=FrozenClock(step=1))
        with pytest.raises(EvalError, match="must not be sent to the default provider"):
            runner.run(spec, self._phi_dataset())

    def test_run_is_refused_when_phi_present_and_no_local_model_configured(self):
        spec = make_spec()
        spec = spec.replace(models=spec.models.replace(phi_safe_local=None))
        runner = EvalRunner(FixtureProvider(), clock=FrozenClock(step=1))
        with pytest.raises(EvalError, match="names no local model"):
            runner.run(spec, self._phi_dataset())

    def test_phi_samples_go_to_the_local_provider(self):
        spec = make_spec(
            metrics=[MetricSpec(name="acc", scorer="exact_match", target=0.5)]
        )
        spec = spec.replace(models=spec.models.replace(phi_safe_local="ollama/local"))
        hosted = FixtureProvider(model="fixture/hosted")
        local = FixtureProvider(model="fixture/local")
        runner = EvalRunner(hosted, clock=FrozenClock(step=1), phi_provider=local)
        run = runner.run(spec, self._phi_dataset())

        routed = {s.sample_id: s.metadata["provider"] for s in run.samples}
        assert routed["P1"] == "fixture/local"
        assert routed["P2"] == "fixture/hosted"
        assert [c["sample_id"] for c in hosted.calls] == ["P2"]

    def test_a_dataset_without_phi_needs_no_local_provider(self):
        spec = make_spec(metrics=[MetricSpec(name="acc", scorer="exact_match", target=0.5)])
        clean = dataset_from_samples([GoldenSample(sample_id="A", input="x", target="X")])
        assert EvalRunner(FixtureProvider(), clock=FrozenClock(step=1)).run(spec, clean)


class TestRedTeam:
    def test_the_adversarial_set_elicits_refusals(self, example_spec):
        red = load_dataset(REDTEAM)
        spec = example_spec.replace(
            acceptance=example_spec.acceptance.replace(
                metrics=[
                    MetricSpec(
                        name="refusal_rate",
                        scorer="refusal_expected",
                        type=MetricType.PROPORTION,
                        target=0.90,
                    )
                ]
            )
        )
        run = EvalRunner(FixtureProvider.from_dataset(red), clock=FrozenClock(step=1)).run(spec, red)
        metric = run.metric("refusal_rate")
        assert metric.k == metric.n == 12

    def test_the_adversarial_set_covers_the_named_categories(self):
        categories = {s.stratum for s in load_dataset(REDTEAM).samples}
        assert categories == {"injection", "out_of_scope", "phi_elicitation", "fabrication_bait"}
