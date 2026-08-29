"""Model providers.

Every model the evaluation runner talks to sits behind one narrow interface, so
that swapping a hosted model for a local one, or a real model for a fixture, is
a configuration change rather than a code change.

:class:`FixtureProvider` deserves explanation, because a deterministic offline
provider might look like a testing shortcut and is not. ValKit's own
qualification has to demonstrate that the harness computes the right acceptance
decision from a known set of outputs, and that demonstration is only possible
if the outputs are known. A fixture provider is therefore how the tool
qualifies itself, how the demonstration runs on a laptop with no credentials,
and how the test suite stays hermetic. It produces a deliberately imperfect
pass rate, because a validation package generated from a flawless run shows
none of the deviation handling that a real one must.

The hosted providers import their SDKs lazily and are never constructed during
tests.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..errors import ProviderError, ValKitError

__all__ = [
    "ProviderResponse",
    "ModelProvider",
    "FixtureProvider",
    "FixtureJudgeProvider",
    "CallableProvider",
    "EchoProvider",
    "RecordingProvider",
    "RetryingProvider",
    "BedrockProvider",
    "OllamaProvider",
    "resolve_provider",
    "provider_for_spec",
    "judge_for_spec",
    "register_provider_scheme",
]


@dataclass
class ProviderResponse:
    """One model call and everything the run record needs to know about it."""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelProvider(Protocol):
    """The interface the runner depends on."""

    @property
    def identity(self) -> str:
        """The model identifier recorded on the run, for installation qualification."""
        ...

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        ...


# --------------------------------------------------------------------------
# Offline providers
# --------------------------------------------------------------------------


class FixtureProvider:
    """A deterministic, offline provider.

    Responses come from an explicit mapping keyed by sample identifier, falling
    back to a rule derived from the sample's target. Failures are injected
    deterministically by identifier so that a demonstration run produces a
    realistic mix of passes, wrong answers and provider errors, and always the
    same mix.
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        model: str = "fixture/deterministic",
        wrong_answer_for: set[str] | None = None,
        error_for: set[str] | None = None,
        refuse_for: set[str] | None = None,
        default_response: str | None = None,
    ):
        self._responses = dict(responses or {})
        self._model = model
        self._wrong = set(wrong_answer_for or ())
        self._errors = set(error_for or ())
        self._refusals = set(refuse_for or ())
        self._default = default_response
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_dataset(cls, dataset: Any, *, model: str = "fixture/deterministic") -> "FixtureProvider":
        """Build a provider whose behaviour is declared by the dataset itself.

        A sample carrying ``metadata.fixture`` of ``"wrong"``, ``"error"`` or
        ``"refuse"`` elicits that behaviour. Keeping the declaration in the
        dataset rather than in the calling code means the demonstration run
        produces the same realistic mixture of passes, wrong answers and
        provider failures wherever it is invoked from, which is what makes the
        generated OQ show deviation handling rather than a suspicious hundred
        percent.
        """
        buckets: dict[str, set[str]] = {"wrong": set(), "error": set(), "refuse": set()}
        for entry in getattr(dataset, "samples", []):
            behaviour = entry.metadata.get("fixture")
            if behaviour in buckets:
                buckets[behaviour].add(entry.sample_id)
        return cls(
            model=model,
            wrong_answer_for=buckets["wrong"],
            error_for=buckets["error"],
            refuse_for=buckets["refuse"],
        )

    @property
    def identity(self) -> str:
        return self._model

    def generate(
        self, prompt: str, *, system: str | None = None, **params: Any
    ) -> ProviderResponse:
        sample_id = params.get("sample_id", "")
        self.calls.append({"prompt": prompt, "system": system, "sample_id": sample_id})

        if sample_id in self._errors:
            raise ProviderError(
                f"fixture provider: simulated failure for sample {sample_id}"
            )

        if sample_id in self._responses:
            text = self._responses[sample_id]
        elif sample_id in self._refusals:
            text = (
                "I cannot help with that request: it falls outside the scope this agent "
                "has been validated for."
            )
        elif sample_id in self._wrong:
            text = self._perturb(params.get("target"), sample_id)
        elif self._default is not None:
            text = self._default
        else:
            text = self._from_target(params.get("target"), prompt)

        return ProviderResponse(
            text=text,
            model=self._model,
            tokens_in=max(1, len(prompt) // 4),
            tokens_out=max(1, len(text) // 4),
            latency_ms=0.0,
            raw={"fixture": True, "sample_id": sample_id},
        )

    @staticmethod
    def _from_target(target: Any, prompt: str) -> str:
        """The correct answer, when the fixture knows one."""
        if target is None:
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
            return f"fixture-response-{digest}"
        if isinstance(target, str):
            return target
        import json

        return json.dumps(target, sort_keys=True)

    @staticmethod
    def _perturb(target: Any, sample_id: str) -> str:
        """A plausible wrong answer, so failures look like real failures.

        A wrong answer that is obviously malformed would be caught by any
        scorer. Realistic failure modes are subtle: a transposed digit, a
        neighbouring field name, a citation to the wrong section.
        """
        text = FixtureProvider._from_target(target, sample_id)
        digits = re.search(r"\d+", text)
        if digits:
            span = digits.group()
            perturbed = str(int(span) + 1).zfill(len(span))
            return text[: digits.start()] + perturbed + text[digits.end() :]
        return text + "_UNVERIFIED"


class FixtureJudgeProvider:
    """A deterministic judge, for offline demonstration and self-qualification.

    Emits the verdict format :mod:`valkit.evals.judge` expects, deciding by
    exact comparison of the candidate against the reference. ``disagree_for``
    names samples where the judge deliberately differs from that comparison,
    which is what stops calibration from returning a meaningless kappa of 1.0:
    a judge that agrees with the reference on every case tells you nothing
    about how it behaves when the reference is unavailable.
    """

    def __init__(
        self,
        *,
        model: str = "fixture/judge",
        disagree_for: set[str] | None = None,
        disagree_every: int | None = None,
    ):
        self._model = model
        self._disagree = set(disagree_for or ())
        # A judge that agrees with the reference on every case would report a
        # Cohen's kappa of 1.0, which is precisely the degenerate result this
        # class exists to avoid: it tells a reader nothing about how the judge
        # behaves where no reference is available. So by default it disagrees on
        # a deterministic fraction of cases, selected by a hash of the sample
        # identifier so the choice is stable across runs and machines.
        #
        # Naming specific cases in disagree_for turns the rate off, because a
        # caller who has said exactly where the judge should differ means those
        # cases and not those plus a background rate. Pass disagree_every
        # explicitly to have both.
        if disagree_every is None:
            disagree_every = 0 if self._disagree else 20
        self._disagree_every = disagree_every

    @property
    def identity(self) -> str:
        return self._model

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        candidate = _section(prompt, "CANDIDATE:")
        reference = _section(prompt, "REFERENCE:")
        acceptable = bool(reference) and candidate.strip() == reference.strip()
        sample_id = params.get("sample_id", "")
        if sample_id in self._disagree or self._selected(sample_id):
            acceptable = not acceptable

        verdict = "ACCEPTABLE" if acceptable else "NOT ACCEPTABLE"
        reason = (
            "The candidate conveys the same substantive content as the reference."
            if acceptable
            else "The candidate differs from the reference in a substantive value."
        )
        return ProviderResponse(
            text=f"VERDICT: {verdict}\nREASON: {reason}",
            model=self._model,
            raw={"fixture": True},
        )


    def _selected(self, sample_id: str) -> bool:
        if not self._disagree_every or not sample_id:
            return False
        digest = hashlib.sha256(sample_id.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self._disagree_every == 0


def _section(prompt: str, header: str) -> str:
    """Pull one labelled section out of the judge prompt."""
    if header not in prompt:
        return ""
    body = prompt.split(header, 1)[1]
    for other in ("TASK:", "REFERENCE:", "CANDIDATE:"):
        if other in body:
            body = body.split(other, 1)[0]
    return body.strip()


class EchoProvider:
    """Returns the prompt. Useful for exercising plumbing."""

    def __init__(self, model: str = "fixture/echo"):
        self._model = model

    @property
    def identity(self) -> str:
        return self._model

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        return ProviderResponse(text=prompt, model=self._model)


class CallableProvider:
    """Wraps any callable as a provider."""

    def __init__(self, function: Callable[..., str], model: str = "callable/custom"):
        self._function = function
        self._model = model

    @property
    def identity(self) -> str:
        return self._model

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        try:
            text = self._function(prompt, system=system, **params)
        except Exception as error:
            raise ProviderError(f"provider callable failed: {error}") from error
        return ProviderResponse(text=str(text), model=self._model)


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------


class RecordingProvider:
    """Wraps a provider and keeps every exchange, for transcript evidence."""

    def __init__(self, inner: ModelProvider):
        self._inner = inner
        self.transcripts: list[dict[str, Any]] = []

    @property
    def identity(self) -> str:
        return self._inner.identity

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        try:
            response = self._inner.generate(prompt, system=system, **params)
        except ProviderError as error:
            self.transcripts.append(
                {"prompt": prompt, "system": system, "error": str(error), **params}
            )
            raise
        self.transcripts.append(
            {"prompt": prompt, "system": system, "response": response.text, **params}
        )
        return response


class RetryingProvider:
    """Retries transient provider failures with deterministic backoff.

    ``sleep`` is injected and defaults to a no-op, so a test exercises the retry
    logic without spending the wall-clock time. A run's determinism is
    unaffected: the same failure sequence produces the same outcome.
    """

    def __init__(
        self,
        inner: ModelProvider,
        *,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] | None = None,
    ):
        if attempts < 1:
            raise ProviderError(f"attempts must be at least 1, got {attempts}")
        self._inner = inner
        self._attempts = attempts
        self._backoff = backoff_seconds
        self._sleep = sleep or (lambda _seconds: None)

    @property
    def identity(self) -> str:
        return self._inner.identity

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        last: Exception | None = None
        for attempt in range(self._attempts):
            try:
                return self._inner.generate(prompt, system=system, **params)
            except ProviderError as error:
                last = error
                if attempt < self._attempts - 1:
                    self._sleep(self._backoff * (2**attempt))
        raise ProviderError(
            f"provider failed after {self._attempts} attempt(s): {last}"
        ) from last


# --------------------------------------------------------------------------
# Hosted providers
# --------------------------------------------------------------------------


class BedrockProvider:
    """Claude on AWS Bedrock.

    Bedrock is the default hosted target because AWS offers a business associate
    agreement covering it, which is what makes it usable where inputs may touch
    protected health information. That is a contractual position, not a
    technical one, and remains the customer's to establish.
    """

    def __init__(self, model_id: str, *, region: str | None = None, client: Any = None):
        self._model_id = model_id
        if client is not None:
            self._client = client
        else:
            try:
                import boto3  # noqa: PLC0415 - deliberately lazy
            except ImportError as error:
                raise ProviderError(
                    "the Bedrock provider requires boto3, which is not installed. "
                    "Install it with: pip install 'valkit[s3]'"
                ) from error
            self._client = boto3.client("bedrock-runtime", region_name=region)

    @property
    def identity(self) -> str:
        return f"bedrock/{self._model_id}"

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        import json

        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": params.get("max_tokens") or 2048,
            "temperature": params.get("temperature", 0.0),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        try:
            response = self._client.invoke_model(
                modelId=self._model_id, body=json.dumps(body)
            )
            payload = json.loads(response["body"].read())
        except Exception as error:
            raise ProviderError(f"Bedrock call failed: {error}") from error

        blocks = payload.get("content", [])
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        usage = payload.get("usage", {})
        return ProviderResponse(
            text=text,
            model=self.identity,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            finish_reason=payload.get("stop_reason", "stop"),
            raw=payload,
        )


class OllamaProvider:
    """A locally hosted model, for evaluation over inputs containing PHI.

    Routing PHI-bearing cases here rather than to a hosted endpoint is the
    control that lets a customer evaluate against real data without a data
    processing agreement covering it. The runner enforces the routing; this
    class only provides the destination.
    """

    def __init__(self, model: str, *, host: str = "http://localhost:11434", timeout: float = 120.0):
        self._model = model
        self._host = host.rstrip("/")
        self._timeout = timeout

    @property
    def identity(self) -> str:
        return f"ollama/{self._model}"

    def generate(self, prompt: str, *, system: str | None = None, **params: Any) -> ProviderResponse:
        import json
        import urllib.error
        import urllib.request

        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": params.get("temperature", 0.0),
                "seed": params.get("seed", 0),
            },
        }
        if system:
            body["system"] = system

        request = urllib.request.Request(
            f"{self._host}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError) as error:
            raise ProviderError(
                f"could not reach the local model at {self._host}: {error}. A local model "
                "is required to evaluate samples containing protected health information."
            ) from error
        except json.JSONDecodeError as error:
            raise ProviderError(f"local model returned invalid JSON: {error}") from error

        return ProviderResponse(
            text=payload.get("response", ""),
            model=self.identity,
            tokens_in=payload.get("prompt_eval_count", 0),
            tokens_out=payload.get("eval_count", 0),
            raw=payload,
        )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

_SCHEMES: dict[str, Callable[[str], ModelProvider]] = {
    "fixture": lambda name: (
        FixtureJudgeProvider() if name == "judge" else FixtureProvider(model=f"fixture/{name}")
    ),
    "echo": lambda name: EchoProvider(model=f"echo/{name}"),
    "bedrock": lambda name: BedrockProvider(name),
    "ollama": lambda name: OllamaProvider(name),
}


def register_provider_scheme(scheme: str, factory: Callable[[str], ModelProvider]) -> None:
    """Register a provider scheme, so a customer can add one without forking."""
    _SCHEMES[scheme] = factory


def resolve_provider(uri: str, registry: dict[str, ModelProvider] | None = None) -> ModelProvider:
    """Turn a specification's model string into a provider.

    ``registry`` takes precedence, which is how a caller injects a configured
    fixture for a model name that would otherwise resolve to a hosted endpoint.
    """
    if registry and uri in registry:
        return registry[uri]
    if "/" not in uri:
        raise ProviderError(
            f"model reference {uri!r} has no scheme; expected something like "
            f"'bedrock/anthropic.claude-sonnet-4' or 'fixture/my-agent'"
        )
    scheme, _, name = uri.partition("/")
    factory = _SCHEMES.get(scheme)
    if factory is None:
        raise ProviderError(
            f"unknown provider scheme {scheme!r}; known schemes are "
            f"{', '.join(sorted(_SCHEMES))}"
        )
    return factory(name)


def provider_for_spec(spec: Any, *, base_dir: Any = None, dataset: Any = None) -> ModelProvider:
    """The primary provider a specification asks for.

    Only one rule sits on top of :func:`resolve_provider`, and it is the reason
    this function exists rather than being written out at each call site: a
    fixture provider has to be built from the golden set, because it takes its
    answers — and which cases it should answer wrongly — from the expected
    outputs in the data. Resolved from the model name alone it would answer
    nothing correctly, and the demonstration it exists to support would report a
    validated agent as failing.

    Pass ``dataset`` when the caller has already loaded it, which avoids reading
    and re-hashing the golden set twice.

    Every entry point that runs a battery goes through here: the CLI, the HTTP
    API, the MCP surface and the worker. What none of them do is substitute a
    fixture for a model the specification named: a run recorded against
    ``bedrock/...`` has to have talked to Bedrock, and a provider factory that
    quietly fell back to fixtures would make a run record a fiction.
    """
    if not spec.models.primary.startswith("fixture/"):
        return resolve_provider(spec.models.primary)

    if dataset is None:
        golden = getattr(getattr(spec, "datasets", None), "golden_set", None)
        if golden is not None:
            from .dataset import load_dataset

            try:
                dataset = load_dataset(golden.ref, base_dir=base_dir)
            except ValKitError:
                # Unreachable, or it fails its pinned digest. Fall through:
                # loading it properly is the runner's job, and the runner
                # reports the failure far better than a provider factory can.
                dataset = None

    if dataset is None:
        return resolve_provider(spec.models.primary)
    return FixtureProvider.from_dataset(dataset, model=spec.models.primary)


def judge_for_spec(spec: Any) -> Any:
    """The judge a specification asks for, or ``None`` if it names none."""
    if not spec.models.judge:
        return None
    from .judge import LlmJudge

    return LlmJudge(provider=resolve_provider(spec.models.judge))
