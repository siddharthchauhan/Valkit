"""Tests for the evidence vault.

As with the audit trail, the interesting tests damage storage directly and
check that the damage is caught. The vault's central claim is that evidence
cannot change after it is written and that any change is detected at the point
of use, so both halves of that claim are attacked here.
"""

from __future__ import annotations

import json
import stat

import pytest

from valkit.errors import IntegrityError, VaultError
from valkit.util import FrozenClock, sha256_text
from valkit.vault.store import EvidenceStore, EvidenceVault


@pytest.fixture
def vault(workdir, clock):
    return EvidenceVault(workdir / "vault", clock)


@pytest.fixture
def populated(vault):
    vault.put_json("metrics", {"k": 176, "n": 180}, agent_id="a1", run_id="RUN-1")
    vault.put_text("transcript", "prompt/response", agent_id="a1", run_id="RUN-1")
    vault.put_json("spec", {"agent_id": "a1"}, agent_id="a1")
    return vault


class TestContentAddressing:
    def test_identifier_is_the_digest(self, vault):
        record = vault.put_text("note", "hello")
        assert record.evidence_id == sha256_text("hello")
        assert record.sha256 == record.evidence_id

    def test_storing_identical_content_twice_is_idempotent(self, vault):
        first = vault.put_json("metrics", {"a": 1})
        second = vault.put_json("metrics", {"a": 1})
        assert first.evidence_id == second.evidence_id
        assert len(vault.records()) == 1

    def test_json_is_canonicalised_so_key_order_does_not_matter(self, vault):
        first = vault.put_json("metrics", {"a": 1, "b": 2})
        second = vault.put_json("metrics", {"b": 2, "a": 1})
        assert first.evidence_id == second.evidence_id

    def test_different_content_gets_a_different_address(self, vault):
        first = vault.put_text("note", "hello")
        second = vault.put_text("note", "world")
        assert first.evidence_id != second.evidence_id
        assert len(vault.records()) == 2

    def test_the_uri_names_the_object(self, vault):
        record = vault.put_text("note", "hello")
        assert record.uri == f"valkit://vault/{record.evidence_id}"

    def test_size_is_recorded(self, vault):
        assert vault.put_text("note", "hello").size_bytes == 5

    def test_rejects_non_bytes(self, vault):
        with pytest.raises(VaultError, match="must be bytes"):
            vault.put_bytes("note", "a string")


class TestRoundTrip:
    def test_text(self, vault):
        record = vault.put_text("note", "a unicode string: éè")
        assert vault.get_text(record.evidence_id) == "a unicode string: éè"

    def test_json(self, vault):
        record = vault.put_json("metrics", {"k": 176, "n": 180})
        assert vault.get_json(record.evidence_id) == {"k": 176, "n": 180}

    def test_file(self, vault, workdir):
        source = workdir / "dataset.jsonl"
        source.write_text('{"id": 1}\n', encoding="utf-8")
        record = vault.put_file("dataset", source)
        assert vault.get_text(record.evidence_id) == '{"id": 1}\n'
        assert record.metadata["filename"] == "dataset.jsonl"

    def test_missing_file(self, vault, workdir):
        with pytest.raises(VaultError, match="not a file"):
            vault.put_file("dataset", workdir / "absent.jsonl")

    def test_unknown_identifier(self, vault):
        with pytest.raises(VaultError, match="no evidence object"):
            vault.get_bytes("0" * 64)


class TestImmutability:
    def test_stored_files_are_read_only(self, vault):
        record = vault.put_text("note", "hello")
        mode = vault._object_path(record.evidence_id).stat().st_mode
        assert not mode & stat.S_IWUSR

    def test_a_tampered_object_is_caught_on_read(self, vault):
        """The central claim: altered evidence never passes as valid."""
        record = vault.put_text("note", "original")
        path = vault._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"tampered")

        with pytest.raises(IntegrityError, match="failed verification on read"):
            vault.get_text(record.evidence_id)

    def test_reads_do_not_trust_the_index(self, vault):
        """Rewriting the index cannot make corrupted content read as valid."""
        record = vault.put_text("note", "original")
        path = vault._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"tampered")

        entries = [json.loads(line) for line in vault.index_path.read_text().splitlines() if line]
        entries[0]["sha256"] = sha256_text("tampered")
        vault.index_path.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )

        with pytest.raises(IntegrityError):
            vault.get_text(record.evidence_id)

    def test_a_corrupted_object_is_detected_on_re_put(self, vault):
        record = vault.put_text("note", "original")
        path = vault._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"tampered")

        with pytest.raises(IntegrityError, match="vault is corrupted"):
            vault.put_text("note", "original")


class TestRetention:
    def test_default_retention_is_ten_years_out(self, vault):
        record = vault.put_text("note", "hello")
        assert record.retention_until.startswith("2036-")

    def test_explicit_retention_is_honoured(self, vault):
        record = vault.put_text("note", "hello", retention_until="2030-01-01T00:00:00Z")
        assert record.retention_until == "2030-01-01T00:00:00Z"

    def test_delete_refuses_under_retention(self, vault):
        record = vault.put_text("note", "hello")
        with pytest.raises(VaultError, match="under retention"):
            vault.delete(record.evidence_id)
        assert vault.exists(record.evidence_id)

    def test_refusal_message_states_that_it_binds_administrators(self, vault):
        record = vault.put_text("note", "hello")
        with pytest.raises(VaultError, match="including for an administrator"):
            vault.delete(record.evidence_id)

    def test_delete_permitted_after_retention(self, vault):
        record = vault.put_text("note", "hello", retention_until="2020-01-01T00:00:00Z")
        vault.delete(record.evidence_id)
        assert not vault.exists(record.evidence_id)

    def test_expired_lists_only_elapsed_records(self, vault):
        old = vault.put_text("a", "old", retention_until="2020-01-01T00:00:00Z")
        vault.put_text("b", "new", retention_until="2040-01-01T00:00:00Z")
        assert [r.evidence_id for r in vault.expired()] == [old.evidence_id]

    def test_purge_defaults_to_a_dry_run(self, vault):
        record = vault.put_text("a", "old", retention_until="2020-01-01T00:00:00Z")
        assert vault.purge_expired() == [record.evidence_id]
        assert vault.exists(record.evidence_id), "a dry run must not delete anything"

    def test_purge_deletes_when_asked(self, vault):
        record = vault.put_text("a", "old", retention_until="2020-01-01T00:00:00Z")
        vault.purge_expired(dry_run=False)
        assert not vault.exists(record.evidence_id)

    def test_purge_never_touches_records_under_retention(self, vault):
        kept = vault.put_text("b", "new")
        vault.put_text("a", "old", retention_until="2020-01-01T00:00:00Z")
        vault.purge_expired(dry_run=False)
        assert vault.exists(kept.evidence_id)

    def test_leap_day_retention_does_not_raise(self, workdir):
        """29 February has no counterpart ten years on."""
        vault = EvidenceVault(workdir / "leap", FrozenClock("2028-02-29T12:00:00Z"))
        record = vault.put_text("note", "hello")
        assert record.retention_until.startswith("2038-02-28")


class TestVerification:
    def test_clean_vault_verifies(self, populated):
        result = populated.verify()
        assert result.ok
        assert result.objects_checked == 3
        assert not result.corrupted and not result.missing

    def test_verification_is_falsy_when_broken(self, populated):
        record = populated.records()[0]
        path = populated._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"corrupted")
        assert not populated.verify()

    def test_corrupted_object_is_named(self, populated):
        record = populated.records()[0]
        path = populated._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"corrupted")
        result = populated.verify()
        assert result.corrupted == [record.evidence_id]
        assert "corrupted" in result.reason

    def test_missing_object_is_named(self, populated):
        record = populated.records()[0]
        path = populated._object_path(record.evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.unlink()
        result = populated.verify()
        assert result.missing == [record.evidence_id]
        assert "missing from storage" in result.reason

    def test_object_with_no_index_entry_is_reported_but_not_fatal(self, populated):
        """An orphan object is a housekeeping matter, not lost evidence."""
        orphan = populated.objects_dir / "ff" / "ff" / ("f" * 64)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        result = populated.verify()
        assert "f" * 64 in result.unindexed
        assert not result.ok or result.corrupted, "an orphan hashes wrong, so it is corrupt"

    def test_verification_works_without_the_index(self, populated):
        """The objects are the source of truth, not the index."""
        populated.index_path.write_text("", encoding="utf-8")
        result = populated.verify()
        assert result.objects_checked == 3
        assert not result.corrupted


class TestManifest:
    def test_manifest_covers_the_selected_evidence(self, populated):
        manifest = populated.manifest(agent_id="a1")
        assert manifest.count == 3
        assert len(manifest.manifest_sha256) == 64

    def test_manifest_can_be_scoped_to_a_run(self, populated):
        assert populated.manifest(agent_id="a1", run_id="RUN-1").count == 2

    def test_manifest_digest_is_deterministic(self, populated):
        assert (
            populated.manifest(agent_id="a1").manifest_sha256
            == populated.manifest(agent_id="a1").manifest_sha256
        )

    def test_manifest_digest_changes_when_evidence_is_added(self, populated):
        before = populated.manifest(agent_id="a1").manifest_sha256
        populated.put_text("extra", "more evidence", agent_id="a1")
        assert populated.manifest(agent_id="a1").manifest_sha256 != before

    def test_manifest_entries_are_ordered_deterministically(self, populated):
        entries = populated.manifest(agent_id="a1").entries
        assert entries == sorted(entries, key=lambda e: (e["kind"], e["evidence_id"]))

    def test_manifest_can_be_stored_as_evidence(self, populated):
        manifest = populated.manifest(agent_id="a1")
        record = populated.store_manifest(manifest)
        assert populated.get_json(record.evidence_id)["manifest_sha256"] == (
            manifest.manifest_sha256
        )


class TestIndex:
    def test_records_are_listed(self, populated):
        assert len(populated.records()) == 3
        assert {r.kind for r in populated.records()} == {"metrics", "transcript", "spec"}

    def test_record_lookup(self, populated):
        record = populated.records()[0]
        assert populated.record(record.evidence_id).kind == record.kind
        assert populated.record("0" * 64) is None

    def test_rebuild_index_from_storage(self, populated):
        populated.index_path.write_text("", encoding="utf-8")
        assert populated.records() == []
        assert populated.rebuild_index() == 3
        assert len(populated.records()) == 3

    def test_rebuilt_entries_declare_what_was_lost(self, populated):
        populated.index_path.write_text("", encoding="utf-8")
        populated.rebuild_index()
        assert all(r.kind == "recovered" for r in populated.records())
        assert all("reconstructed" in r.metadata.get("note", "") for r in populated.records())

    def test_total_bytes(self, populated):
        assert populated.total_bytes() == sum(r.size_bytes for r in populated.records())


class TestProtocol:
    def test_local_vault_satisfies_the_store_protocol(self, vault):
        assert isinstance(vault, EvidenceStore)

    def test_s3_vault_satisfies_the_store_protocol(self):
        from valkit.vault.s3 import S3EvidenceVault

        stub = S3EvidenceVault("bucket", client=_FakeS3())
        assert isinstance(stub, EvidenceStore)


class _FakeS3:
    """Minimal in-process stand-in for the S3 client. No network, no boto3."""

    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if key in self.objects:
            existing = self.objects[key]
            if existing.get("ObjectLockMode") == "COMPLIANCE":
                raise AssertionError("Object Lock would refuse an overwrite")
        self.objects[key] = dict(kwargs)
        return {"ETag": "fake"}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key]["Body"])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key]["Body"])}

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        contents = [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data
