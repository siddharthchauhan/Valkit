"""Datasets, providers, scorers, the LLM judge, and the evaluation runner.

The runner is the apparatus a validation package attributes its results to, so
it records what it was: harness version, provider identity, model parameters,
seed and dataset digests all go onto the run for installation qualification.
"""

from __future__ import annotations

from .dataset import (
    DatasetLoadResult,
    DatasetSummary,
    dataset_from_samples,
    load_dataset,
    load_dataset_detailed,
    non_phi_samples,
    phi_samples,
    sample,
    stratify,
    summarise,
    write_dataset,
)
from .judge import DEFAULT_RUBRIC, LlmJudge, calibrate
from .providers import (
    BedrockProvider,
    CallableProvider,
    EchoProvider,
    FixtureJudgeProvider,
    FixtureProvider,
    ModelProvider,
    OllamaProvider,
    ProviderResponse,
    RecordingProvider,
    RetryingProvider,
    register_provider_scheme,
    resolve_provider,
)
from .runner import HARNESS_VERSION, EvalRunner, RunOptions
from .scorers import ScorerRegistry, available_scorers, get_scorer, register, registry

__all__ = [
    "EvalRunner",
    "RunOptions",
    "HARNESS_VERSION",
    "load_dataset",
    "load_dataset_detailed",
    "DatasetLoadResult",
    "DatasetSummary",
    "dataset_from_samples",
    "write_dataset",
    "stratify",
    "summarise",
    "sample",
    "phi_samples",
    "non_phi_samples",
    "ModelProvider",
    "ProviderResponse",
    "FixtureProvider",
    "FixtureJudgeProvider",
    "EchoProvider",
    "CallableProvider",
    "RecordingProvider",
    "RetryingProvider",
    "BedrockProvider",
    "OllamaProvider",
    "resolve_provider",
    "register_provider_scheme",
    "LlmJudge",
    "calibrate",
    "DEFAULT_RUBRIC",
    "ScorerRegistry",
    "registry",
    "register",
    "get_scorer",
    "available_scorers",
]
