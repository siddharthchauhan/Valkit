"""LLM-as-judge, and the calibration that makes it admissible.

Using a model to grade a model is the obvious way to score open-ended output at
scale, and it is the first thing a quality function objects to. The objection is
sound as usually posed: an ungrounded judge is an unvalidated measuring
instrument, and evidence produced by an unvalidated instrument is not evidence.

The answer is not to argue that the judge is good. It is to measure how well it
agrees with qualified human assessment on a labelled subset, report that
agreement with its diagnostics, and block sign-off when agreement falls below a
threshold the customer sets. The judge is then admissible on the same footing as
any other instrument: within a stated accuracy, demonstrated by comparison
against a reference.

Two consequences are enforced here rather than left to the caller.

*Too few labels is a calibration failure, not a pass.* A judge compared against
eight labelled cases has not been calibrated, and the estimate of agreement is
too imprecise to rely on. Falling back to "passed" when there is insufficient
evidence is exactly the failure mode the whole product exists to prevent.

*An unparseable verdict is an error, not a pass.* If the judge's response cannot
be read, the sample is recorded as errored and excluded from the denominator,
where it is visible, rather than being scored as acceptable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..errors import EvalError, ProviderError
from ..models import GoldenSample, JudgeCalibration, JudgeCalibrationSpec, Score
from ..stats.agreement import summarise_agreement
from .providers import ModelProvider

__all__ = ["LlmJudge", "calibrate", "DEFAULT_RUBRIC", "JUDGE_SYSTEM_PROMPT"]


DEFAULT_RUBRIC = """\
Judge whether the CANDIDATE answer is acceptable, given the TASK and the
REFERENCE answer.

Acceptable means the candidate conveys the same substantive content as the
reference. Differences in wording, formatting or ordering do not make an answer
unacceptable. A difference in any value, identifier, quantity or citation does.

If the candidate omits information the reference contains, or contains a claim
the reference does not support, it is not acceptable.
"""

JUDGE_SYSTEM_PROMPT = """\
You are grading the output of a system operating in a regulated environment.

Answer with a single line in exactly this form:

VERDICT: ACCEPTABLE
or
VERDICT: NOT ACCEPTABLE

followed by one line beginning 'REASON:' giving a brief, specific justification
that names the difference you relied on.

Do not hedge and do not produce any other output. If you cannot determine
whether the candidate is acceptable, answer NOT ACCEPTABLE and say why in the
reason.
"""

_VERDICT_PATTERN = re.compile(
    r"VERDICT\s*:\s*(ACCEPTABLE|NOT\s+ACCEPTABLE)", re.IGNORECASE
)
_REASON_PATTERN = re.compile(r"REASON\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)


@dataclass
class LlmJudge:
    """Scores an output by asking a model, then records how it was asked."""

    provider: ModelProvider
    rubric: str = DEFAULT_RUBRIC
    system_prompt: str = JUDGE_SYSTEM_PROMPT
    temperature: float = 0.0
    seed: int = 0

    @property
    def identity(self) -> str:
        return self.provider.identity

    def build_prompt(self, sample: GoldenSample, output: str) -> str:
        """The exact prompt sent to the judge, retained as evidence.

        The prompt is part of the measuring instrument. An OQ that reports a
        judge's verdicts without recording how the judge was asked has not
        described its instrument.
        """
        parts = [
            self.rubric,
            "",
            "TASK:",
            str(sample.input),
            "",
            "REFERENCE:",
            _render(sample.target),
            "",
            "CANDIDATE:",
            output,
        ]
        return "\n".join(parts)

    def score(self, sample: GoldenSample, output: str, **options: Any) -> Score:
        prompt = self.build_prompt(sample, output)
        try:
            response = self.provider.generate(
                prompt,
                system=self.system_prompt,
                temperature=self.temperature,
                seed=self.seed,
                sample_id=sample.sample_id,
                target=sample.target,
            )
        except ProviderError as error:
            raise EvalError(f"the judge failed on sample {sample.sample_id}: {error}") from error

        verdict = _VERDICT_PATTERN.search(response.text)
        if verdict is None:
            raise EvalError(
                f"the judge returned an unreadable verdict for sample "
                f"{sample.sample_id}: {response.text[:160]!r}. An unparseable verdict is "
                "recorded as an error, never as a pass."
            )

        acceptable = verdict.group(1).upper().replace(" ", "").replace("\t", "") == "ACCEPTABLE"
        reason_match = _REASON_PATTERN.search(response.text)
        reason = reason_match.group(1).strip().split("\n")[0] if reason_match else ""

        return Score(
            value=1.0 if acceptable else 0.0,
            passed=acceptable,
            explanation=(
                f"Judge ({self.provider.identity}) verdict: "
                f"{'acceptable' if acceptable else 'not acceptable'}."
                + (f" {reason}" if reason else "")
            ),
            scorer="judge",
            metadata={
                "judge_model": self.provider.identity,
                "reason": reason,
                "prompt_sha256_prefix": _short_digest(prompt),
            },
        )

    def __call__(self, sample: GoldenSample, output: str, **options: Any) -> Score:
        return self.score(sample, output, **options)


def _render(value: Any) -> str:
    if value is None:
        return "(no reference answer provided)"
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


def _short_digest(text: str) -> str:
    from ..util import sha256_text

    return sha256_text(text)[:16]


def calibrate(
    judge_scores: dict[str, Score],
    human_labels: dict[str, float],
    spec: JudgeCalibrationSpec | None = None,
    judge_model: str = "",
    confidence: float = 0.95,
) -> JudgeCalibration:
    """Compare the judge against human labels on the overlapping samples.

    Returns a failing calibration rather than raising when there are too few
    labelled cases: an under-calibrated judge is a finding to report in the
    credibility assessment, not an exception to swallow.
    """
    spec = spec or JudgeCalibrationSpec()

    shared = sorted(set(judge_scores) & set(human_labels))
    n = len(shared)

    if n == 0:
        return JudgeCalibration(
            judge_model=judge_model,
            n=0,
            cohen_kappa=0.0,
            percent_agreement=0.0,
            min_required=spec.min_cohen_kappa,
            passed=False,
            note=(
                "No sample carries both a judge verdict and a human label, so agreement "
                "could not be computed. The judge is uncalibrated and its verdicts do not "
                "constitute evidence."
            ),
        )

    reference = [1.0 if human_labels[sample_id] >= 0.5 else 0.0 for sample_id in shared]
    prediction = [1.0 if judge_scores[sample_id].passed else 0.0 for sample_id in shared]
    summary = summarise_agreement(reference, prediction, confidence)

    notes: list[str] = []
    passed = True

    if n < spec.min_samples:
        passed = False
        notes.append(
            f"Calibrated against {n} labelled case(s), fewer than the {spec.min_samples} "
            f"the specification requires. A judge compared against too few cases has not "
            f"been calibrated, and this is recorded as a failure rather than a pass."
        )

    if summary.kappa < spec.min_cohen_kappa:
        passed = False
        notes.append(
            f"Cohen's kappa is {summary.kappa:.3f}, below the required "
            f"{spec.min_cohen_kappa:.2f}. The judge does not agree with human assessment "
            f"closely enough for its verdicts to support an acceptance claim."
        )

    if spec.min_percent_agreement is not None and summary.percent_agreement < spec.min_percent_agreement:
        passed = False
        notes.append(
            f"Percent agreement is {summary.percent_agreement:.1%}, below the required "
            f"{spec.min_percent_agreement:.1%}."
        )

    if passed:
        notes.append(
            f"Cohen's kappa {summary.kappa:.3f} ({summary.interpretation}) over {n} "
            f"labelled case(s), against a required minimum of {spec.min_cohen_kappa:.2f}. "
            f"Percent agreement {summary.percent_agreement:.1%}. "
            f"95% confidence interval for kappa [{summary.lower:.3f}, {summary.upper:.3f}]."
        )

    notes.extend(summary.caveats)

    return JudgeCalibration(
        judge_model=judge_model,
        n=n,
        cohen_kappa=summary.kappa,
        percent_agreement=summary.percent_agreement,
        min_required=spec.min_cohen_kappa,
        passed=passed,
        confusion=summary.confusion,
        note=" ".join(notes),
    )
