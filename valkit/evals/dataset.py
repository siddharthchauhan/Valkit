"""Golden sets and adversarial sets.

The golden set is where a validation package's statistical argument is either
earned or lost. The binomial machinery in :mod:`valkit.stats` assumes the test
cases are independent and representative of the intended use; nothing in the
arithmetic can establish that, and a lower confidence bound computed over an
unrepresentative sample is precise and meaningless. Curating and stratifying
the set is the human work that makes the numbers defensible, and
:func:`summarise` exists so the credibility report can show that the work was
done rather than assert it.

Two digests are computed for every dataset, because they answer different
questions:

*The canonical digest* covers the parsed samples in canonical JSON form. It is
invariant to formatting, line endings and key order, so it answers "is this the
same data?" — which is what reproducibility of a result depends on, and what
survives a checkout on a different platform.

*The file digest* covers the raw bytes. It answers "is this the same file?",
which is what a validation engineer computes with ``sha256sum`` and what an
inspector can recompute unaided.

A pin in the specification is checked against either, so a team that pinned the
obvious thing is never wrong-footed, and the error names both.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..errors import DatasetError
from ..models import Dataset, GoldenSample
from ..util import sha256_bytes, sha256_obj

__all__ = [
    "DatasetLoadResult",
    "DatasetSummary",
    "load_dataset",
    "load_dataset_detailed",
    "dataset_from_samples",
    "write_dataset",
    "stratify",
    "summarise",
    "sample",
    "phi_samples",
    "non_phi_samples",
    "PHI_METADATA_KEYS",
]

# Metadata keys that mark a sample as containing protected health information.
# Overridable per call, because every organisation names these differently.
PHI_METADATA_KEYS = frozenset({"phi", "contains_phi", "has_phi", "pii", "contains_pii"})

_SAMPLE_KEYS = {
    "sample_id",
    "id",
    "input",
    "target",
    "expected",
    "metadata",
    "contains_phi",
    "stratum",
    "human_label",
    "tags",
}


@dataclass(frozen=True)
class DatasetLoadResult:
    """A loaded dataset together with both of its digests."""

    dataset: Dataset
    canonical_sha256: str
    file_sha256: str | None
    path: str | None = None


@dataclass(frozen=True)
class DatasetSummary:
    """What the credibility report says about the qualification set."""

    name: str
    n: int
    labelled: int
    phi: int
    strata: dict[str, int] = field(default_factory=dict)
    tags: dict[str, int] = field(default_factory=dict)
    duplicate_inputs: int = 0
    notes: list[str] = field(default_factory=list)


def _parse_sample(raw: Any, index: int, source: str) -> GoldenSample:
    if not isinstance(raw, dict):
        raise DatasetError(
            f"{source}: entry {index} is a {type(raw).__name__}, expected an object"
        )

    unknown = set(raw) - _SAMPLE_KEYS
    if unknown:
        # Unknown keys are kept rather than rejected: a golden set often carries
        # domain columns the harness has no opinion about, and losing them would
        # break stratification the customer defined.
        pass

    sample_id = raw.get("sample_id") or raw.get("id")
    if not sample_id:
        sample_id = f"S-{index + 1:04d}"

    if "input" not in raw:
        raise DatasetError(f"{source}: entry {index} ({sample_id}) has no 'input'")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise DatasetError(f"{source}: entry {index} ({sample_id}) has a non-object 'metadata'")
    for key, value in raw.items():
        if key not in _SAMPLE_KEYS:
            metadata.setdefault(key, value)

    human_label = raw.get("human_label")
    if human_label is not None:
        if isinstance(human_label, bool):
            human_label = 1.0 if human_label else 0.0
        elif isinstance(human_label, (int, float)):
            human_label = float(human_label)
        else:
            raise DatasetError(
                f"{source}: entry {index} ({sample_id}) has a non-numeric 'human_label': "
                f"{human_label!r}"
            )

    contains_phi = bool(raw.get("contains_phi", False))
    if not contains_phi:
        contains_phi = any(bool(metadata.get(key)) for key in PHI_METADATA_KEYS)

    return GoldenSample(
        sample_id=str(sample_id),
        input=raw["input"],
        target=raw.get("target", raw.get("expected")),
        metadata=metadata,
        contains_phi=contains_phi,
        stratum=raw.get("stratum") or (str(metadata["form"]) if "form" in metadata else None),
        human_label=human_label,
        tags=list(raw.get("tags", [])),
    )


def _parse_lines(text: str, source: str) -> list[GoldenSample]:
    """Parse JSONL, reporting the line number of a malformed entry."""
    samples: list[GoldenSample] = []
    index = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise DatasetError(
                f"{source}: line {line_number} is not valid JSON: {error.msg}"
            ) from error
        samples.append(_parse_sample(raw, index, f"{source} line {line_number}"))
        index += 1
    return samples


def _parse_document(text: str, source: str) -> list[GoldenSample]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise DatasetError(f"{source}: not valid JSON: {error.msg}") from error
    if isinstance(document, dict):
        document = document.get("samples", document.get("data"))
    if not isinstance(document, list):
        raise DatasetError(
            f"{source}: expected a JSON array of samples, or an object with a 'samples' array"
        )
    return [_parse_sample(raw, index, source) for index, raw in enumerate(document)]


def _validate(samples: Sequence[GoldenSample], source: str) -> None:
    if not samples:
        raise DatasetError(
            f"{source}: the dataset is empty. An acceptance criterion cannot be "
            "demonstrated against no cases."
        )
    counts = Counter(s.sample_id for s in samples)
    duplicates = sorted(sid for sid, count in counts.items() if count > 1)
    if duplicates:
        raise DatasetError(
            f"{source}: duplicate sample identifier(s) {', '.join(duplicates[:5])}"
            f"{'...' if len(duplicates) > 5 else ''}. Identifiers must be unique so that a "
            "result can be traced to the case that produced it."
        )


def load_dataset_detailed(
    ref: str | os.PathLike[str],
    *,
    name: str | None = None,
    expected_sha256: str | None = None,
    base_dir: str | os.PathLike[str] | None = None,
    version: str | None = None,
) -> DatasetLoadResult:
    """Load a dataset from JSONL or JSON, returning both digests."""
    path = Path(ref)
    if base_dir is not None and not path.is_absolute():
        path = Path(base_dir) / path

    if not path.exists():
        raise DatasetError(
            f"dataset not found: {path}. Remote references (s3://, https://) must be "
            "fetched to a local path before loading."
        )
    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise DatasetError(f"could not read dataset {path}: {error}") from error

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DatasetError(
            f"{path}: is not valid UTF-8 at byte {error.start}. Datasets must be UTF-8 so "
            "that their digest is stable across platforms."
        ) from error

    source = str(path)
    samples = (
        _parse_lines(text, source)
        if path.suffix in (".jsonl", ".ndjson")
        else _parse_document(text, source)
    )
    _validate(samples, source)

    canonical = sha256_obj(samples)
    file_digest = sha256_bytes(raw_bytes)

    if expected_sha256:
        pinned = expected_sha256.lower()
        if pinned not in (canonical, file_digest):
            raise DatasetError(
                f"{path}: dataset digest does not match the pinned value.\n"
                f"  pinned    : {pinned}\n"
                f"  canonical : {canonical}  (digest of the parsed data)\n"
                f"  file      : {file_digest}  (digest of the raw bytes)\n"
                "The qualification dataset is not the one the validation plan approved."
            )

    dataset = Dataset(
        name=name or path.stem,
        ref=str(ref),
        sha256=canonical,
        samples=samples,
        version=version,
        description=f"Loaded from {path.name}",
    )
    return DatasetLoadResult(
        dataset=dataset, canonical_sha256=canonical, file_sha256=file_digest, path=str(path)
    )


def load_dataset(
    ref: str | os.PathLike[str],
    *,
    name: str | None = None,
    expected_sha256: str | None = None,
    base_dir: str | os.PathLike[str] | None = None,
    version: str | None = None,
) -> Dataset:
    """Load a dataset from JSONL or JSON."""
    return load_dataset_detailed(
        ref, name=name, expected_sha256=expected_sha256, base_dir=base_dir, version=version
    ).dataset


def dataset_from_samples(
    samples: Sequence[GoldenSample],
    *,
    name: str = "inline",
    ref: str = "inline",
    version: str | None = None,
) -> Dataset:
    """Build a dataset from samples already in memory."""
    _validate(samples, name)
    return Dataset(
        name=name, ref=ref, sha256=sha256_obj(list(samples)), samples=list(samples), version=version
    )


def write_dataset(dataset: Dataset, path: str | os.PathLike[str]) -> str:
    """Write a dataset as JSONL and return the file digest."""
    lines = []
    for entry in dataset.samples:
        record: dict[str, Any] = {"sample_id": entry.sample_id, "input": entry.input}
        if entry.target is not None:
            record["target"] = entry.target
        if entry.metadata:
            record["metadata"] = entry.metadata
        if entry.contains_phi:
            record["contains_phi"] = True
        if entry.stratum:
            record["stratum"] = entry.stratum
        if entry.human_label is not None:
            record["human_label"] = entry.human_label
        if entry.tags:
            record["tags"] = entry.tags
        lines.append(json.dumps(record, sort_keys=True, ensure_ascii=False))

    body = "\n".join(lines) + "\n"
    Path(path).write_text(body, encoding="utf-8")
    return sha256_bytes(body.encode("utf-8"))


# --------------------------------------------------------------------------
# Views over a dataset
# --------------------------------------------------------------------------


def phi_samples(dataset: Dataset) -> list[GoldenSample]:
    return [s for s in dataset.samples if s.contains_phi]


def non_phi_samples(dataset: Dataset) -> list[GoldenSample]:
    return [s for s in dataset.samples if not s.contains_phi]


def stratify(dataset: Dataset, key: str = "stratum") -> dict[str, list[GoldenSample]]:
    """Group samples by a stratification key.

    ``"stratum"`` uses the dedicated field; any other key is read from metadata.
    Samples with no value for the key are grouped under ``"(unspecified)"``
    rather than silently dropped, because a stratum that vanishes from the
    breakdown is a stratum nobody notices is untested.
    """
    groups: dict[str, list[GoldenSample]] = defaultdict(list)
    for entry in dataset.samples:
        if key == "stratum":
            value = entry.stratum
        else:
            raw = entry.metadata.get(key)
            value = None if raw is None else str(raw)
        groups[value or "(unspecified)"].append(entry)
    return dict(sorted(groups.items()))


def sample(dataset: Dataset, n: int, *, seed: int = 0, stratified_by: str | None = None) -> Dataset:
    """Take a deterministic subsample.

    Seeded from a local :class:`random.Random`, never the global module, so a
    subsample is reproducible and cannot be perturbed by unrelated code drawing
    from the shared generator.
    """
    if n <= 0:
        raise DatasetError(f"subsample size must be positive, got {n}")
    if n >= len(dataset.samples):
        return dataset

    generator = random.Random(seed)
    if stratified_by is None:
        chosen = sorted(
            generator.sample(dataset.samples, n), key=lambda s: s.sample_id
        )
    else:
        groups = stratify(dataset, stratified_by)
        chosen = []
        total = len(dataset.samples)
        for value, members in groups.items():
            take = max(1, round(n * len(members) / total))
            take = min(take, len(members))
            chosen.extend(generator.sample(members, take))
        chosen = sorted(chosen, key=lambda s: s.sample_id)[:n]

    return Dataset(
        name=f"{dataset.name}-sample-{n}",
        ref=dataset.ref,
        sha256=sha256_obj(chosen),
        samples=chosen,
        version=dataset.version,
        description=f"Deterministic subsample of {dataset.name} (n={n}, seed={seed})",
    )


def summarise(dataset: Dataset, strata_keys: Iterable[str] = ("stratum",)) -> DatasetSummary:
    """Describe the qualification set for the credibility report.

    The notes are the honest part. A binomial acceptance argument rests on the
    set being representative and the cases independent; where the composition
    gives reason to doubt that, it is better said in the report than left for a
    reviewer to notice.
    """
    samples = dataset.samples
    n = len(samples)
    labelled = sum(1 for s in samples if s.human_label is not None)
    phi = sum(1 for s in samples if s.contains_phi)

    strata: dict[str, int] = {}
    for key in strata_keys:
        for value, members in stratify(dataset, key).items():
            strata[f"{key}={value}" if key != "stratum" else value] = len(members)

    tags = Counter(tag for s in samples for tag in s.tags)

    normalised = [
        re.sub(r"\s+", " ", json.dumps(s.input, sort_keys=True)).strip().lower() for s in samples
    ]
    duplicate_inputs = n - len(set(normalised))

    notes: list[str] = []
    if duplicate_inputs:
        notes.append(
            f"{duplicate_inputs} sample(s) share an input with another sample. Repeated "
            "cases are not independent observations and inflate the apparent evidence "
            "base; the confidence bound assumes independence."
        )
    if labelled == 0:
        notes.append(
            "No sample carries a human label, so an LLM judge cannot be calibrated against "
            "this set."
        )
    elif labelled < 20:
        notes.append(
            f"Only {labelled} sample(s) carry a human label. Judge calibration on a small "
            "labelled subset gives an imprecise estimate of agreement."
        )
    if len(strata) == 1 and n > 20:
        notes.append(
            "All samples fall in a single stratum. A set that does not span the intended "
            "range of use cannot support a claim about that range."
        )
    if phi:
        notes.append(
            f"{phi} sample(s) are flagged as containing protected health information and "
            "will be routed to a local model."
        )

    return DatasetSummary(
        name=dataset.name,
        n=n,
        labelled=labelled,
        phi=phi,
        strata=strata,
        tags=dict(sorted(tags.items())),
        duplicate_inputs=duplicate_inputs,
        notes=notes,
    )
