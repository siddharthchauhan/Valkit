"""The immutable evidence vault.

Every transcript, dataset, metric set, specification and generated document is
written here and never changes again. In production this is S3 with Object Lock
in Compliance mode, where not even the account root can delete an object before
its retention date expires; the local implementation enforces the same
semantics so that behaviour in a developer's test matches behaviour in a
regulated deployment.

Storage is content-addressed: an object lives at a path derived from the
SHA-256 of its bytes, and **the identifier is the digest**. That has three
consequences worth stating, because they are what the design buys:

*Integrity is structural.* An identifier that resolves is itself proof that the
bytes are the ones that were stored. There is no separate checksum to keep in
step, and :meth:`EvidenceVault.get_bytes` re-derives the digest on every read
rather than trusting the index.

*Writes are idempotent.* Storing the same content twice yields the same
identifier and the same object. Nothing is duplicated and nothing is
overwritten.

*Overwriting is impossible rather than merely forbidden.* Different content
necessarily lands at a different address, so there is no operation that
replaces an object's bytes. The retention policy therefore only has to guard
deletion.

As with the audit trail's triggers, the read-only file permissions set here are
a guard rail against accident, not a security boundary. The digest is the
control.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from ..errors import IntegrityError, VaultError
from ..models import EvidenceRecord
from ..util import (
    Clock,
    SystemClock,
    canonical_json,
    format_utc,
    parse_utc,
    sha256_bytes,
    sha256_text,
)

__all__ = [
    "EvidenceStore",
    "EvidenceVault",
    "VaultVerification",
    "EvidenceManifest",
    "DEFAULT_RETENTION_YEARS",
]

# Ten years is a common floor for clinical trial records; the correct figure is
# jurisdiction- and record-specific and belongs in the customer's retention
# policy, not in a library default. This value only applies when no explicit
# retention is supplied.
DEFAULT_RETENTION_YEARS = 10


@dataclass(frozen=True)
class VaultVerification:
    """The outcome of re-hashing every object in the vault."""

    ok: bool
    objects_checked: int
    corrupted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unindexed: list[str] = field(default_factory=list)
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class EvidenceManifest:
    """A sealed listing of evidence, itself a signable object.

    One signature over a manifest covers an arbitrary number of artefacts,
    which is what makes signing a validation package practical: an approver
    signs one record whose digest commits to every transcript, dataset and
    document beneath it.
    """

    manifest_id: str
    generated_at: str
    agent_id: str | None
    run_id: str | None
    entries: list[dict[str, Any]]
    manifest_sha256: str

    @property
    def count(self) -> int:
        return len(self.entries)


@runtime_checkable
class EvidenceStore(Protocol):
    """The interface the pipeline uses, satisfied by both local and S3 vaults."""

    def put_bytes(self, kind: str, data: bytes, **kwargs: Any) -> EvidenceRecord: ...

    def get_bytes(self, evidence_id: str) -> bytes: ...

    def exists(self, evidence_id: str) -> bool: ...

    def records(self) -> list[EvidenceRecord]: ...

    def verify(self) -> VaultVerification: ...


class EvidenceVault:
    """A local, content-addressed, write-once evidence vault."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        clock: Clock | None = None,
        *,
        retention_years: int = DEFAULT_RETENTION_YEARS,
        read_only_files: bool = True,
    ):
        self.root = Path(root)
        self._clock = clock or SystemClock()
        self.retention_years = retention_years
        self._read_only_files = read_only_files
        self.objects_dir = self.root / "objects"
        self.index_path = self.root / "index.jsonl"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.touch(exist_ok=True)

    # -- paths -------------------------------------------------------------

    def _object_path(self, digest: str) -> Path:
        """Fan out on the first four hex characters to keep directories small."""
        return self.objects_dir / digest[:2] / digest[2:4] / digest

    # -- writing -----------------------------------------------------------

    def put_bytes(
        self,
        kind: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        retention_until: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> EvidenceRecord:
        """Store bytes and return the record describing them.

        Storing identical content again is a no-op that returns the existing
        record: the address is the content, so there is nothing to change.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise VaultError(f"evidence must be bytes, got {type(data).__name__}")

        digest = sha256_bytes(bytes(data))
        path = self._object_path(digest)

        if path.exists():
            # The object is already here. Confirm it has not rotted on disk
            # before reporting success, because a silent bad read would let a
            # corrupted artefact pass as verified evidence.
            existing = path.read_bytes()
            if sha256_bytes(existing) != digest:
                raise IntegrityError(
                    f"the stored object at {path} does not match its address {digest}; "
                    "the vault is corrupted and must be investigated before use"
                )
            existing_record = self._index_lookup(digest)
            if existing_record is not None:
                return existing_record
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.parent / f".{digest}.partial"
            temporary.write_bytes(bytes(data))
            # Atomic publish: a reader never observes a partially written object.
            temporary.replace(path)
            if self._read_only_files:
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        record = EvidenceRecord(
            evidence_id=digest,
            kind=kind,
            sha256=digest,
            size_bytes=len(data),
            stored_at=self._clock.now_iso(),
            uri=f"valkit://vault/{digest}",
            content_type=content_type,
            retention_until=retention_until or self._default_retention(),
            agent_id=agent_id,
            run_id=run_id,
            metadata=dict(metadata or {}),
        )
        self._append_index(record)
        return record

    def put_text(self, kind: str, text: str, *, content_type: str = "text/plain", **kwargs: Any) -> EvidenceRecord:
        return self.put_bytes(kind, text.encode("utf-8"), content_type=content_type, **kwargs)

    def put_json(self, kind: str, value: Any, **kwargs: Any) -> EvidenceRecord:
        """Store an object as canonical JSON, so equal objects share an address."""
        kwargs.setdefault("content_type", "application/json")
        return self.put_bytes(kind, canonical_json(value).encode("utf-8"), **kwargs)

    def put_file(self, kind: str, path: str | os.PathLike[str], **kwargs: Any) -> EvidenceRecord:
        source = Path(path)
        if not source.is_file():
            raise VaultError(f"not a file: {source}")
        kwargs.setdefault("metadata", {}).setdefault("filename", source.name)
        return self.put_bytes(kind, source.read_bytes(), **kwargs)

    def _default_retention(self) -> str:
        now = self._clock.now()
        try:
            until = now.replace(year=now.year + self.retention_years)
        except ValueError:
            # 29 February in a leap year has no counterpart in the target year.
            until = now.replace(year=now.year + self.retention_years, day=28)
        return format_utc(until)

    # -- index -------------------------------------------------------------

    def _append_index(self, record: EvidenceRecord) -> None:
        with open(self.index_path, "a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")

    def _index_entries(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        entries = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    def _index_lookup(self, evidence_id: str) -> EvidenceRecord | None:
        for entry in self._index_entries():
            if entry["evidence_id"] == evidence_id:
                return _record_from(entry)
        return None

    def records(self) -> list[EvidenceRecord]:
        """Every record in the index, most recent last, de-duplicated by id."""
        seen: dict[str, EvidenceRecord] = {}
        for entry in self._index_entries():
            seen[entry["evidence_id"]] = _record_from(entry)
        return list(seen.values())

    def record(self, evidence_id: str) -> EvidenceRecord | None:
        return self._index_lookup(evidence_id)

    def exists(self, evidence_id: str) -> bool:
        return self._object_path(evidence_id).exists()

    # -- reading -----------------------------------------------------------

    def get_bytes(self, evidence_id: str) -> bytes:
        """Read an object, re-deriving its digest before returning it.

        The index is a convenience, never an authority: verification here reads
        the bytes and re-derives the address, so a corrupted or substituted
        object is caught at the point of use rather than at the next audit.
        """
        path = self._object_path(evidence_id)
        if not path.exists():
            raise VaultError(f"no evidence object with identifier {evidence_id}")
        data = path.read_bytes()
        actual = sha256_bytes(data)
        if actual != evidence_id:
            raise IntegrityError(
                f"evidence object {evidence_id} failed verification on read: its content "
                f"hashes to {actual}. The object has been altered or corrupted."
            )
        return data

    def get_text(self, evidence_id: str) -> str:
        return self.get_bytes(evidence_id).decode("utf-8")

    def get_json(self, evidence_id: str) -> Any:
        return json.loads(self.get_text(evidence_id))

    # -- retention and deletion --------------------------------------------

    def expired(self, now: str | None = None) -> list[EvidenceRecord]:
        """Records whose retention period has elapsed."""
        moment = parse_utc(now) if now else self._clock.now()
        out = []
        for record in self.records():
            if record.retention_until and parse_utc(record.retention_until) <= moment:
                out.append(record)
        return out

    def delete(self, evidence_id: str, *, now: str | None = None) -> None:
        """Delete one object, refusing while it remains under retention."""
        record = self._index_lookup(evidence_id)
        moment = parse_utc(now) if now else self._clock.now()
        if record is not None and record.retention_until:
            until = parse_utc(record.retention_until)
            if until > moment:
                raise VaultError(
                    f"evidence {evidence_id} is under retention until "
                    f"{record.retention_until} and cannot be deleted. Under a compliance "
                    f"retention mode this refusal is absolute, including for an "
                    f"administrator."
                )
        path = self._object_path(evidence_id)
        if path.exists():
            path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            path.unlink()

    def purge_expired(self, *, dry_run: bool = True, now: str | None = None) -> list[str]:
        """Delete objects past their retention date.

        Defaults to a dry run. Destroying validation evidence is not something
        that should happen because an argument was omitted.
        """
        targets = [record.evidence_id for record in self.expired(now)]
        if not dry_run:
            for evidence_id in targets:
                self.delete(evidence_id, now=now)
        return targets

    # -- verification ------------------------------------------------------

    def verify(self) -> VaultVerification:
        """Re-hash every object and reconcile it against the index.

        Works from the objects on disk, so it remains meaningful even if the
        index is lost: the index is reported against, not relied upon.
        """
        corrupted: list[str] = []
        missing: list[str] = []
        checked = 0

        indexed = {record.evidence_id for record in self.records()}
        on_disk: set[str] = set()

        for path in self.objects_dir.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            on_disk.add(path.name)
            checked += 1
            if sha256_bytes(path.read_bytes()) != path.name:
                corrupted.append(path.name)

        for evidence_id in sorted(indexed - on_disk):
            missing.append(evidence_id)

        unindexed = sorted(on_disk - indexed)
        ok = not corrupted and not missing

        if ok:
            reason = "all objects verified against their digests"
        else:
            parts = []
            if corrupted:
                parts.append(f"{len(corrupted)} corrupted object(s)")
            if missing:
                parts.append(f"{len(missing)} indexed object(s) missing from storage")
            reason = "; ".join(parts)

        return VaultVerification(
            ok=ok,
            objects_checked=checked,
            corrupted=sorted(corrupted),
            missing=missing,
            unindexed=unindexed,
            reason=reason,
        )

    # -- manifests ---------------------------------------------------------

    def manifest(
        self, agent_id: str | None = None, run_id: str | None = None
    ) -> EvidenceManifest:
        """Seal a set of evidence into one signable listing."""
        selected = [
            record
            for record in self.records()
            if (agent_id is None or record.agent_id == agent_id)
            and (run_id is None or record.run_id == run_id)
        ]
        entries = sorted(
            (
                {
                    "evidence_id": record.evidence_id,
                    "kind": record.kind,
                    "sha256": record.sha256,
                    "size_bytes": record.size_bytes,
                    "stored_at": record.stored_at,
                    "content_type": record.content_type,
                    "retention_until": record.retention_until,
                }
                for record in selected
            ),
            key=lambda entry: (entry["kind"], entry["evidence_id"]),
        )
        body = {"agent_id": agent_id, "run_id": run_id, "entries": entries}
        digest = sha256_text(canonical_json(body))
        return EvidenceManifest(
            manifest_id=f"MANIFEST-{digest[:16]}",
            generated_at=self._clock.now_iso(),
            agent_id=agent_id,
            run_id=run_id,
            entries=entries,
            manifest_sha256=digest,
        )

    def store_manifest(self, manifest: EvidenceManifest, **kwargs: Any) -> EvidenceRecord:
        """Store a manifest in the vault, so the seal is itself evidence."""
        return self.put_json(
            "manifest",
            {
                "manifest_id": manifest.manifest_id,
                "generated_at": manifest.generated_at,
                "agent_id": manifest.agent_id,
                "run_id": manifest.run_id,
                "entries": manifest.entries,
                "manifest_sha256": manifest.manifest_sha256,
            },
            agent_id=manifest.agent_id,
            run_id=manifest.run_id,
            **kwargs,
        )

    # -- maintenance -------------------------------------------------------

    def rebuild_index(self) -> int:
        """Reconstruct the index from the objects on disk.

        The objects are the source of truth; this exists so that a lost or
        truncated index is a recoverable inconvenience rather than a loss of
        evidence. Metadata that lived only in the index cannot be recovered,
        and the rebuilt entries say so.
        """
        recovered: list[EvidenceRecord] = []
        known = {record.evidence_id: record for record in self.records()}
        for path in sorted(self.objects_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name in known:
                recovered.append(known[path.name])
                continue
            recovered.append(
                EvidenceRecord(
                    evidence_id=path.name,
                    kind="recovered",
                    sha256=path.name,
                    size_bytes=path.stat().st_size,
                    stored_at=self._clock.now_iso(),
                    uri=f"valkit://vault/{path.name}",
                    metadata={"note": "index entry reconstructed from storage"},
                )
            )
        self.index_path.write_text(
            "".join(record.to_json() + "\n" for record in recovered), encoding="utf-8"
        )
        return len(recovered)

    def total_bytes(self) -> int:
        return sum(record.size_bytes for record in self.records())

    def destroy(self) -> None:
        """Remove the entire vault. Intended for test teardown only."""
        if self.root.exists():
            for path in self.root.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            shutil.rmtree(self.root)


def _record_from(entry: dict[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=entry["evidence_id"],
        kind=entry["kind"],
        sha256=entry["sha256"],
        size_bytes=entry["size_bytes"],
        stored_at=entry["stored_at"],
        uri=entry["uri"],
        content_type=entry.get("content_type", "application/json"),
        retention_until=entry.get("retention_until"),
        agent_id=entry.get("agent_id"),
        run_id=entry.get("run_id"),
        metadata=entry.get("metadata", {}),
    )
