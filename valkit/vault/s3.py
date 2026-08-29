"""Evidence storage on S3 with Object Lock.

S3 Object Lock in Compliance mode is the strongest write-once guarantee
available on commodity infrastructure: for the duration of the retention
period, no principal can delete or overwrite the object version, including the
account root user. That property is what makes it the right home for validation
evidence, and it is also what makes it dangerous — a mistaken hundred-year
retention on a large object cannot be undone, and the storage will be billed
for a hundred years.

Bucket prerequisites, none of which this class can create for you:

*Object Lock must be enabled at bucket creation.* It cannot be turned on for an
existing bucket. A deployment that discovers this late has to create a new
bucket and copy, so it belongs in the infrastructure plan from the start.

*Versioning must be enabled*, and is enabled implicitly by Object Lock.

*A default retention configuration is advisable* so that an object written by a
path that forgets to set retention is still protected.

Governance mode is the alternative to Compliance mode: it allows a principal
holding ``s3:BypassGovernanceRetention`` to shorten or remove retention. That
is appropriate for a staging environment and is not appropriate for records
supporting a regulatory submission, so this class defaults to Compliance and
requires the caller to opt out explicitly.

``boto3`` is imported lazily, so ``import valkit.vault`` works without it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..errors import IntegrityError, VaultError
from ..models import EvidenceRecord
from ..util import (
    Clock,
    SystemClock,
    canonical_json,
    format_utc,
    parse_utc,
    sha256_bytes,
)
from .store import DEFAULT_RETENTION_YEARS, VaultVerification

__all__ = ["S3EvidenceVault"]


class S3EvidenceVault:
    """Content-addressed evidence storage backed by S3 Object Lock.

    Satisfies the same interface as :class:`~valkit.vault.store.EvidenceVault`,
    so the pipeline is indifferent to which is in use.

    ``client`` exists for testing: passing a stub avoids importing boto3 and
    avoids any network access, which is how this class is exercised in the test
    suite.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "evidence/",
        *,
        client: Any = None,
        clock: Clock | None = None,
        retention_years: int = DEFAULT_RETENTION_YEARS,
        object_lock_mode: str = "COMPLIANCE",
        kms_key_id: str | None = None,
        region: str | None = None,
    ):
        if object_lock_mode not in ("COMPLIANCE", "GOVERNANCE"):
            raise VaultError(
                f"object_lock_mode must be 'COMPLIANCE' or 'GOVERNANCE', "
                f"got {object_lock_mode!r}"
            )
        self.bucket = bucket
        self.prefix = prefix if prefix.endswith("/") or not prefix else prefix + "/"
        self.retention_years = retention_years
        self.object_lock_mode = object_lock_mode
        self.kms_key_id = kms_key_id
        self._clock = clock or SystemClock()
        self._client = client if client is not None else self._make_client(region)

    @staticmethod
    def _make_client(region: str | None) -> Any:
        try:
            import boto3  # noqa: PLC0415 - deliberately lazy
        except ImportError as error:
            raise VaultError(
                "S3 evidence storage requires boto3, which is not installed. "
                "Install it with: pip install 'valkit[s3]'"
            ) from error
        return boto3.client("s3", region_name=region) if region else boto3.client("s3")

    # -- keys --------------------------------------------------------------

    def _key(self, digest: str) -> str:
        return f"{self.prefix}objects/{digest[:2]}/{digest[2:4]}/{digest}"

    def _retain_until(self) -> str:
        now = self._clock.now()
        # Adding years by day count avoids the 29 February edge case entirely.
        return format_utc(now + timedelta(days=365 * self.retention_years))

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
        digest = sha256_bytes(bytes(data))
        key = self._key(digest)
        retain_until = retention_until or self._retain_until()

        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": bytes(data),
            "ContentType": content_type,
            "ChecksumSHA256": _b64_digest(digest),
            "ObjectLockMode": self.object_lock_mode,
            "ObjectLockRetainUntilDate": parse_utc(retain_until),
            "Metadata": {
                "valkit-kind": kind,
                "valkit-sha256": digest,
                **({"valkit-agent-id": agent_id} if agent_id else {}),
                **({"valkit-run-id": run_id} if run_id else {}),
            },
        }
        if self.kms_key_id:
            request["ServerSideEncryption"] = "aws:kms"
            request["SSEKMSKeyId"] = self.kms_key_id
        else:
            request["ServerSideEncryption"] = "AES256"

        self._client.put_object(**request)

        record = EvidenceRecord(
            evidence_id=digest,
            kind=kind,
            sha256=digest,
            size_bytes=len(data),
            stored_at=self._clock.now_iso(),
            uri=f"s3://{self.bucket}/{key}",
            content_type=content_type,
            retention_until=retain_until,
            agent_id=agent_id,
            run_id=run_id,
            metadata=dict(metadata or {}),
        )
        self._put_index_entry(record)
        return record

    def put_text(self, kind: str, text: str, *, content_type: str = "text/plain", **kwargs: Any) -> EvidenceRecord:
        return self.put_bytes(kind, text.encode("utf-8"), content_type=content_type, **kwargs)

    def put_json(self, kind: str, value: Any, **kwargs: Any) -> EvidenceRecord:
        kwargs.setdefault("content_type", "application/json")
        return self.put_bytes(kind, canonical_json(value).encode("utf-8"), **kwargs)

    def _put_index_entry(self, record: EvidenceRecord) -> None:
        """Write a per-object index entry.

        S3 has no append, so the index is one small object per record under an
        ``index/`` prefix rather than a single appended file. Listing that
        prefix reconstructs the index, and each entry is itself immutable.
        """
        self._client.put_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}index/{record.evidence_id}.json",
            Body=record.to_json().encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    # -- reading -----------------------------------------------------------

    def get_bytes(self, evidence_id: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._key(evidence_id))
        except Exception as error:
            raise VaultError(f"could not read evidence {evidence_id}: {error}") from error
        body = response["Body"]
        data = body.read() if hasattr(body, "read") else bytes(body)
        actual = sha256_bytes(data)
        if actual != evidence_id:
            raise IntegrityError(
                f"evidence object {evidence_id} failed verification on read: its content "
                f"hashes to {actual}"
            )
        return data

    def get_text(self, evidence_id: str) -> str:
        return self.get_bytes(evidence_id).decode("utf-8")

    def get_json(self, evidence_id: str) -> Any:
        return json.loads(self.get_text(evidence_id))

    def exists(self, evidence_id: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(evidence_id))
            return True
        except Exception:
            return False

    def records(self) -> list[EvidenceRecord]:
        from .store import _record_from

        out: list[EvidenceRecord] = []
        token: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": f"{self.prefix}index/",
            }
            if token:
                request["ContinuationToken"] = token
            response = self._client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                body = self._client.get_object(Bucket=self.bucket, Key=item["Key"])["Body"]
                raw = body.read() if hasattr(body, "read") else bytes(body)
                out.append(_record_from(json.loads(raw.decode("utf-8"))))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
        return out

    # -- verification and deletion ----------------------------------------

    def verify(self) -> VaultVerification:
        """Re-read every indexed object and check it against its address.

        This downloads everything it checks. On a large vault that is slow and
        chargeable, which is why the local implementation is the one used in
        routine qualification and this exists for periodic assurance.
        """
        corrupted: list[str] = []
        missing: list[str] = []
        checked = 0
        for record in self.records():
            try:
                self.get_bytes(record.evidence_id)
                checked += 1
            except VaultError:
                # VaultError is a subclass of IntegrityError, so it must be
                # caught first: an object that is absent has not failed
                # verification, and conflating the two would report a deleted
                # object as a corrupted one.
                missing.append(record.evidence_id)
            except IntegrityError:
                corrupted.append(record.evidence_id)
                checked += 1

        ok = not corrupted and not missing
        return VaultVerification(
            ok=ok,
            objects_checked=checked,
            corrupted=sorted(corrupted),
            missing=sorted(missing),
            reason=(
                "all objects verified against their digests"
                if ok
                else f"{len(corrupted)} corrupted, {len(missing)} missing"
            ),
        )

    def delete(self, evidence_id: str, **_: Any) -> None:
        """Always refuses.

        Under Compliance-mode Object Lock the service itself rejects the
        deletion, so an attempt would produce an opaque ``AccessDenied`` from
        AWS. Failing here instead makes the reason explicit, and makes the
        intended lifecycle clear: retention expires, it is not revoked.
        """
        raise VaultError(
            f"evidence {evidence_id} is held under S3 Object Lock in "
            f"{self.object_lock_mode} mode and cannot be deleted before its retention "
            "date. Objects leave the vault by retention expiry alone."
        )


def _b64_digest(hex_digest: str) -> str:
    """S3 expects the checksum base64-encoded rather than hex."""
    import base64

    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
