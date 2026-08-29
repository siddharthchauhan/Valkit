"""The evaluation runner.

One run of the acceptance battery: load the pinned dataset, call the model on
each case, apply the scorers the acceptance criteria name, store transcripts as
evidence, compute the bounds, calibrate the judge, and record enough about the
apparatus that the result can be attributed and repeated.

Three behaviours here are safety properties rather than features.

*Protected health information never reaches a hosted provider by accident.*
When the specification names a local model for PHI and the dataset contains
PHI-flagged samples, the runner refuses to start unless a local provider was
actually supplied. A quiet fallback to the default provider would be the worst
defect this product could have, so it is a hard failure with an explicit
message, not a warning.

*A missing scorer fails before any model is called.* Resolving every scorer the
acceptance criteria name is done up front, so a typo costs nothing rather than
being discovered after an entire battery has been paid for.

*The apparatus is recorded.* Harness version, provider identity, model
parameters, seed, dataset digests and a configuration digest all go onto the
run. Installation qualification checks these; without them a result cannot be
attributed to a known configuration.
"""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..audit.store import AuditTrail
from ..errors import EvalError, ProviderError
from ..models import (
    AgentSpec,
    Dataset,
    EvalRun,
    GoldenSample,
    HarnessInfo,
    MetricSpec,
    RunStatus,
    SampleResult,
    Score,
)
from ..stats.acceptance import evaluate_acceptance
from ..util import Clock, SystemClock, canonical_json, sha256_text
from .judge import LlmJudge, calibrate
from .providers import ModelProvider, ProviderResponse
from .scorers import ScorerRegistry, registry as default_registry

__all__ = ["EvalRunner", "RunOptions", "HARNESS_NAME", "HARNESS_VERSION"]

HARNESS_NAME = "valkit"
HARNESS_VERSION = "0.1.0"


@dataclass
class RunOptions:
    """Knobs that do not belong in the specification."""

    max_error_rate: float = 0.10
    """Abort the run when more than this fraction of samples fail to execute.

    A run riddled with provider errors is not a failed acceptance test, it is a
    broken apparatus, and reporting it as an acceptance failure would attribute
    an infrastructure problem to the agent.
    """

    store_transcripts: bool = True
    prompt_template: str | None = None
    system_prompt: str | None = None
    stop_on_first_error: bool = False


class EvalRunner:
    """Executes an acceptance battery against an agent specification."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        judge: LlmJudge | None = None,
        scorers: ScorerRegistry | None = None,
        clock: Clock | None = None,
        vault: Any = None,
        audit: AuditTrail | None = None,
        phi_provider: ModelProvider | None = None,
        options: RunOptions | None = None,
    ):
        self._provider = provider
        self._judge = judge
        self._scorers = scorers or default_registry
        self._clock = clock or SystemClock()
        self._vault = vault
        self._audit = audit
        self._phi_provider = phi_provider
        self._options = options or RunOptions()
        self._run_counter = 0

    # -- pre-flight --------------------------------------------------------

    def _check_phi_routing(self, spec: AgentSpec, dataset: Dataset) -> None:
        phi_count = dataset.phi_count
        if not phi_count:
            return
        if self._phi_provider is not None:
            return
        if spec.models.phi_safe_local:
            raise EvalError(
                f"{phi_count} sample(s) in {dataset.name!r} are flagged as containing "
                f"protected health information, and the specification names "
                f"{spec.models.phi_safe_local!r} to evaluate them locally, but no local "
                f"provider was supplied to the runner. The run is refused: PHI must not "
                f"be sent to the default provider."
            )
        raise EvalError(
            f"{phi_count} sample(s) in {dataset.name!r} are flagged as containing "
            f"protected health information, but the specification names no local model "
            f"(models.phi_safe_local) and no local provider was supplied. Either remove "
            f"the PHI from the qualification set, or configure a local model."
        )

    def _resolve_scorers(self, spec: AgentSpec) -> dict[str, Callable[..., Score]]:
        """Resolve every scorer the acceptance criteria name, before any model call."""
        resolved: dict[str, Callable[..., Score]] = {}
        for metric in spec.acceptance.metrics:
            name = metric.scorer_name
            if name in resolved:
                continue
            if name == "judge":
                if self._judge is None:
                    raise EvalError(
                        f"metric {metric.name!r} is scored by the judge, but no judge was "
                        "configured on the runner"
                    )
                resolved[name] = self._judge.score
            else:
                resolved[name] = self._scorers.get(name)
        return resolved

    # -- running -----------------------------------------------------------

    def run(
        self,
        spec: AgentSpec,
        dataset: Dataset,
        *,
        run_id: str | None = None,
        dataset_file_sha256: str | None = None,
        metrics: Sequence[MetricSpec] | None = None,
    ) -> EvalRun:
        """Execute the battery and return a complete run record."""
        self._check_phi_routing(spec, dataset)
        scorers = self._resolve_scorers(spec)
        strata_keys = self._strata_keys(spec)

        self._run_counter += 1
        run_id = run_id or f"RUN-{self._clock.now().strftime('%Y%m%d')}-{self._run_counter:04d}"
        started_at = self._clock.now_iso()

        harness = self._build_harness(spec, dataset, scorers)
        self._record_audit(
            spec.metadata.owner or "system",
            "run.started",
            "run",
            run_id,
            {
                "agent_id": spec.agent_id,
                "agent_version": spec.version,
                "dataset": dataset.ref,
                "dataset_sha256": dataset.sha256,
                "model": self._provider.identity,
                "harness_config_sha256": harness.config_sha256,
            },
        )

        results: list[SampleResult] = []
        errors = 0
        for sample in dataset.samples:
            result = self._run_sample(spec, sample, scorers, run_id, strata_keys)
            if result.error:
                errors += 1
                if self._options.stop_on_first_error:
                    results.append(result)
                    break
            results.append(result)

        status = RunStatus.COMPLETED
        error_message: str | None = None
        if dataset.samples and errors / len(dataset.samples) > self._options.max_error_rate:
            status = RunStatus.FAILED
            error_message = (
                f"{errors} of {len(dataset.samples)} samples failed to execute "
                f"({errors / len(dataset.samples):.0%}), exceeding the maximum error rate "
                f"of {self._options.max_error_rate:.0%}. This is an apparatus failure, not "
                f"an acceptance failure: the agent has not been evaluated."
            )

        metric_specs = list(metrics) if metrics is not None else spec.acceptance.metrics
        metric_results = (
            evaluate_acceptance(spec.acceptance.replace(metrics=list(metric_specs)), results)
            if status is RunStatus.COMPLETED
            else []
        )

        calibration = None
        if self._judge is not None and spec.acceptance.judge_calibration is not None:
            calibration = self._calibrate(spec, dataset, results)

        transcripts_ref = None
        if self._vault is not None and self._options.store_transcripts:
            record = self._vault.put_json(
                "transcripts",
                [r.to_dict() for r in results],
                agent_id=spec.agent_id,
                run_id=run_id,
            )
            transcripts_ref = record.evidence_id

        run = EvalRun(
            run_id=run_id,
            agent_id=spec.agent_id,
            agent_version=spec.version,
            dataset_ref=dataset.ref,
            dataset_sha256=dataset.sha256,
            model=self._provider.identity,
            status=status,
            started_at=started_at,
            finished_at=self._clock.now_iso(),
            spec_sha256=spec.source_sha256,
            judge_model=self._judge.identity if self._judge else None,
            seed=spec.models.seed,
            samples=results,
            metrics=metric_results,
            calibration=calibration,
            harness=harness,
            environment={
                "dataset_file_sha256": dataset_file_sha256,
                "dataset_canonical_sha256": dataset.sha256,
                "dataset_n": len(dataset.samples),
                "phi_samples": dataset.phi_count,
                "temperature": spec.models.temperature,
                "scorers": sorted(scorers),
            },
            transcripts_ref=transcripts_ref,
            error=error_message,
        )

        for metric in metric_results:
            self._record_audit(
                spec.metadata.owner or "system",
                "run.metric_evaluated",
                "run",
                run_id,
                {
                    "metric": metric.name,
                    "k": metric.k,
                    "n": metric.n,
                    "lower_bound": metric.lower_bound,
                    "target": metric.target,
                    "passed": metric.passed,
                },
            )
        self._record_audit(
            spec.metadata.owner or "system",
            "run.completed",
            "run",
            run_id,
            {
                "status": status.value,
                "passed": run.passed,
                "errors": errors,
                "transcripts_ref": transcripts_ref,
            },
        )
        return run

    @staticmethod
    def _strata_keys(spec: AgentSpec) -> list[str]:
        """Every metadata key some metric wants its results broken down by.

        These are copied from the golden sample onto the result, because the
        acceptance engine stratifies over the result rather than the sample.
        Only the declared keys are copied: a golden set often carries bulky
        fields such as the full source text, and duplicating those into every
        result would bloat the run record and its stored transcript for no
        benefit.
        """
        keys: list[str] = []
        for metric in spec.acceptance.metrics:
            for key in metric.strata:
                if key != "stratum" and key not in keys:
                    keys.append(key)
        return keys

    def _run_sample(
        self,
        spec: AgentSpec,
        sample: GoldenSample,
        scorers: dict[str, Callable[..., Score]],
        run_id: str,
        strata_keys: Sequence[str] = (),
    ) -> SampleResult:
        provider = self._phi_provider if sample.contains_phi else self._provider
        prompt = self._build_prompt(sample)

        start = time.perf_counter()
        try:
            response = provider.generate(
                prompt,
                system=self._options.system_prompt,
                sample_id=sample.sample_id,
                target=sample.target,
                temperature=spec.models.temperature,
                seed=spec.models.seed,
                max_tokens=spec.models.max_tokens,
                **spec.models.parameters,
            )
        except ProviderError as error:
            return SampleResult(
                sample_id=sample.sample_id,
                output="",
                scores={},
                stratum=sample.stratum,
                error=str(error),
                metadata={
                    "provider": provider.identity,
                    "phi_routed": sample.contains_phi,
                    **{k: sample.metadata[k] for k in strata_keys if k in sample.metadata},
                },
            )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        scores: dict[str, Score] = {}
        score_error: str | None = None
        for name, scorer in scorers.items():
            options = self._scorer_options(spec, name)
            try:
                scores[name] = scorer(sample, response.text, **options)
            except EvalError as error:
                # A judge that cannot be read is an execution error, so the
                # sample leaves the denominator rather than counting as a pass.
                score_error = str(error)
            except Exception as error:  # pragma: no cover - scorer bug
                raise EvalError(
                    f"scorer {name!r} raised on sample {sample.sample_id}: {error}. "
                    "Scorers must return a failing score rather than raise."
                ) from error

        return SampleResult(
            sample_id=sample.sample_id,
            output=response.text,
            scores=scores,
            stratum=sample.stratum,
            latency_ms=elapsed_ms if not self._deterministic() else 0.0,
            error=score_error,
            metadata={
                "provider": provider.identity,
                "phi_routed": sample.contains_phi,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "finish_reason": response.finish_reason,
                **{k: sample.metadata[k] for k in strata_keys if k in sample.metadata},
            },
        )

    def _deterministic(self) -> bool:
        """Whether timings should be suppressed for byte-stable run records.

        Real latency is genuine evidence about the apparatus but makes a run
        record non-reproducible. With a fixture provider there is no meaningful
        latency to record, and reproducibility is worth more.
        """
        return self._provider.identity.startswith("fixture/")

    def _build_prompt(self, sample: GoldenSample) -> str:
        if self._options.prompt_template:
            return self._options.prompt_template.format(
                input=sample.input, **{k: v for k, v in sample.metadata.items() if isinstance(v, str)}
            )
        source = sample.metadata.get("source_text")
        if source:
            return f"{sample.input}\n\nSOURCE:\n{source}"
        return str(sample.input)

    @staticmethod
    def _scorer_options(spec: AgentSpec, scorer_name: str) -> dict[str, Any]:
        """Tolerances and thresholds a metric declares, passed to its scorer."""
        options: dict[str, Any] = {}
        for metric in spec.acceptance.metrics:
            if metric.scorer_name != scorer_name:
                continue
            if metric.tolerance_abs is not None:
                options["tolerance_abs"] = metric.tolerance_abs
            if metric.tolerance_rel is not None:
                options["tolerance_rel"] = metric.tolerance_rel
        return options

    def _calibrate(self, spec: AgentSpec, dataset: Dataset, results: list[SampleResult]):
        """Calibrate the judge against the human-labelled subset.

        When a metric is scored by the judge its verdicts already exist and are
        reused. When none is — a specification may qualify a judge it intends to
        use for production monitoring while scoring the OQ deterministically —
        the judge is applied here, to the labelled subset only. Calibration is
        the qualification of a measuring instrument and is worth doing whether
        or not that instrument gates this particular run; restricting it to the
        labelled cases keeps the cost proportionate, since unlabelled cases
        contribute nothing to an agreement statistic.
        """
        labels = {
            sample.sample_id: sample.human_label
            for sample in dataset.samples
            if sample.human_label is not None
        }
        judge_scores = {
            result.sample_id: result.scores["judge"]
            for result in results
            if "judge" in result.scores
        }

        if not judge_scores and labels and self._judge is not None:
            by_id = {sample.sample_id: sample for sample in dataset.samples}
            outputs = {result.sample_id: result for result in results}
            for sample_id in labels:
                sample = by_id.get(sample_id)
                result = outputs.get(sample_id)
                if sample is None or result is None or result.error is not None:
                    continue
                try:
                    judge_scores[sample_id] = self._judge.score(sample, result.output)
                except EvalError:
                    # An unreadable verdict leaves the sample out of the
                    # agreement calculation rather than counting as agreement.
                    continue

        return calibrate(
            judge_scores,
            labels,
            spec.acceptance.judge_calibration,
            judge_model=self._judge.identity if self._judge else "",
        )

    def _build_harness(
        self, spec: AgentSpec, dataset: Dataset, scorers: dict[str, Any]
    ) -> HarnessInfo:
        """Identify the apparatus, for installation qualification.

        The configuration digest covers everything that could change a result:
        the specification, the dataset, the scorers in play, the model
        parameters and the seed. Two runs with the same digest that disagree
        indicate non-determinism somewhere, which is itself a finding.
        """
        config = {
            "harness": HARNESS_NAME,
            "harness_version": HARNESS_VERSION,
            "spec_sha256": spec.source_sha256,
            "dataset_sha256": dataset.sha256,
            "model": self._provider.identity,
            "judge": self._judge.identity if self._judge else None,
            "phi_provider": self._phi_provider.identity if self._phi_provider else None,
            "scorers": sorted(scorers),
            "temperature": spec.models.temperature,
            "seed": spec.models.seed,
            "max_tokens": spec.models.max_tokens,
            "parameters": spec.models.parameters,
        }
        return HarnessInfo(
            name=HARNESS_NAME,
            version=HARNESS_VERSION,
            provider=self._provider.identity,
            config_sha256=sha256_text(canonical_json(config)),
            python_version=".".join(str(part) for part in sys.version_info[:3]),
            platform=platform.system(),
        )

    def _record_audit(
        self, actor: str, action: str, entity_type: str, entity_id: str, payload: dict[str, Any]
    ) -> None:
        if self._audit is not None:
            self._audit.append(actor, action, entity_type, entity_id, payload)
