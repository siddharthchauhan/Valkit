"""Tests for the S3 Object Lock evidence vault.

Entirely offline: a stub client stands in for boto3, so these tests assert the
requests ValKit *makes* rather than what AWS does with them. That is the right
boundary for a unit test, and the properties being checked — that Compliance
mode and a retention date are always set, that the checksum is sent, that
deletion is refused — are exactly the ones a misconfiguration would silently
drop.
"""

from __future__ import annotations

import base64
import json

import pytest

from valkit.errors import IntegrityError, VaultError
from valkit.util import FrozenClock, parse_utc, sha256_text
from valkit.vault.s3 import S3EvidenceVault


class FakeS3:
    """In-process stand-in for the S3 client, modelling Object Lock refusal."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        key = kwargs["Key"]
        existing = self.objects.get(key)
        if existing is not None and existing.get("ObjectLockMode") == "COMPLIANCE":
            if existing["Body"] != kwargs["Body"]:
                raise PermissionError("AccessDenied: object is locked in COMPLIANCE mode")
        self.objects[key] = dict(kwargs)
        return {"ETag": "fake"}

    def get_object(self, Bucket, Key):
        self.calls.append(("get_object", {"Key": Key}))
        if Key not in self.objects:
            raise FileNotFoundError(f"NoSuchKey: {Key}")
        return {"Body": _Body(self.objects[Key]["Body"])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FileNotFoundError(f"NoSuchKey: {Key}")
        return {"ContentLength": len(self.objects[Key]["Body"])}

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        return {
            "Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
            "IsTruncated": False,
        }


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture
def client():
    return FakeS3()


@pytest.fixture
def vault(client, clock):
    return S3EvidenceVault("evidence-bucket", client=client, clock=clock)


class TestObjectLock:
    def test_every_write_sets_compliance_mode(self, vault, client):
        vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ObjectLockMode"] == "COMPLIANCE"

    def test_every_write_sets_a_retention_date(self, vault, client):
        vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ObjectLockRetainUntilDate"] > parse_utc("2035-01-01T00:00:00Z")

    def test_governance_mode_must_be_chosen_explicitly(self, client):
        vault = S3EvidenceVault("b", client=client, object_lock_mode="GOVERNANCE")
        vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ObjectLockMode"] == "GOVERNANCE"

    def test_invalid_lock_mode_rejected(self, client):
        with pytest.raises(VaultError, match="COMPLIANCE"):
            S3EvidenceVault("b", client=client, object_lock_mode="NONE")

    def test_checksum_is_sent_so_s3_verifies_the_upload(self, vault, client):
        record = vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ChecksumSHA256"] == base64.b64encode(
            bytes.fromhex(record.sha256)
        ).decode("ascii")

    def test_encryption_is_always_requested(self, vault, client):
        vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ServerSideEncryption"] == "AES256"

    def test_kms_key_is_used_when_configured(self, client):
        vault = S3EvidenceVault("b", client=client, kms_key_id="arn:aws:kms:key/abc")
        vault.put_text("note", "hello")
        put = next(kwargs for name, kwargs in client.calls if name == "put_object")
        assert put["ServerSideEncryption"] == "aws:kms"
        assert put["SSEKMSKeyId"] == "arn:aws:kms:key/abc"


class TestAddressingAndRoundTrip:
    def test_key_is_derived_from_the_digest(self, vault):
        record = vault.put_text("note", "hello")
        digest = sha256_text("hello")
        assert record.uri == (
            f"s3://evidence-bucket/evidence/objects/{digest[:2]}/{digest[2:4]}/{digest}"
        )

    def test_round_trip(self, vault):
        record = vault.put_json("metrics", {"k": 176, "n": 180})
        assert vault.get_json(record.evidence_id) == {"k": 176, "n": 180}

    def test_exists(self, vault):
        record = vault.put_text("note", "hello")
        assert vault.exists(record.evidence_id)
        assert not vault.exists("0" * 64)

    def test_missing_object_raises_a_vault_error_not_a_client_error(self, vault):
        with pytest.raises(VaultError, match="could not read evidence"):
            vault.get_bytes("0" * 64)

    def test_tampered_object_is_caught_on_read(self, vault, client):
        record = vault.put_text("note", "hello")
        key = vault._key(record.evidence_id)
        client.objects[key]["Body"] = b"tampered"
        with pytest.raises(IntegrityError, match="failed verification on read"):
            vault.get_bytes(record.evidence_id)

    def test_prefix_is_normalised(self, client):
        vault = S3EvidenceVault("b", prefix="custom", client=client)
        assert vault._key("ab" + "0" * 62).startswith("custom/objects/")


class TestIndex:
    def test_an_index_entry_is_written_alongside_each_object(self, vault, client):
        record = vault.put_text("note", "hello")
        assert f"evidence/index/{record.evidence_id}.json" in client.objects

    def test_records_are_reconstructed_by_listing_the_index_prefix(self, vault):
        vault.put_json("metrics", {"a": 1}, agent_id="a1")
        vault.put_text("transcript", "t", agent_id="a1")
        records = vault.records()
        assert len(records) == 2
        assert {r.kind for r in records} == {"metrics", "transcript"}

    def test_index_entry_is_valid_json(self, vault, client):
        record = vault.put_text("note", "hello")
        raw = client.objects[f"evidence/index/{record.evidence_id}.json"]["Body"]
        assert json.loads(raw.decode("utf-8"))["evidence_id"] == record.evidence_id


class TestVerificationAndDeletion:
    def test_clean_vault_verifies(self, vault):
        vault.put_text("a", "one")
        vault.put_text("b", "two")
        result = vault.verify()
        assert result.ok
        assert result.objects_checked == 2

    def test_corrupted_object_is_reported(self, vault, client):
        record = vault.put_text("a", "one")
        client.objects[vault._key(record.evidence_id)]["Body"] = b"corrupted"
        result = vault.verify()
        assert not result.ok
        assert result.corrupted == [record.evidence_id]

    def test_missing_object_is_reported(self, vault, client):
        record = vault.put_text("a", "one")
        del client.objects[vault._key(record.evidence_id)]
        result = vault.verify()
        assert not result.ok
        assert result.missing == [record.evidence_id]

    def test_delete_always_refuses_and_explains_why(self, vault):
        record = vault.put_text("a", "one")
        with pytest.raises(VaultError, match="Object Lock"):
            vault.delete(record.evidence_id)

    def test_delete_message_states_the_intended_lifecycle(self, vault):
        record = vault.put_text("a", "one")
        with pytest.raises(VaultError, match="retention expiry alone"):
            vault.delete(record.evidence_id)


class TestWithoutBoto3:
    def test_constructing_without_boto3_gives_a_clear_error(self, monkeypatch):
        """The absence of an optional extra must not surface as an ImportError."""
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("No module named 'boto3'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(VaultError, match=r"pip install 'valkit\[s3\]'"):
            S3EvidenceVault("bucket")

    def test_the_module_imports_without_boto3(self):
        """Referencing the class must never require the extra."""
        import valkit.vault

        assert valkit.vault.S3EvidenceVault is S3EvidenceVault
