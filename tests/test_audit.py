"""Tests for the audit trail.

Most of this file attacks the trail rather than exercising it. A hash chain
that has never been shown to detect tampering is decoration; each test below
damages the database through a raw connection, bypassing every Python-level
control, and asserts that verification localises the damage to the right
record.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from valkit.audit.store import GENESIS_HASH, AuditTrail, compute_row_hash
from valkit.errors import AuditError
from valkit.util import FrozenClock


@pytest.fixture
def trail(clock):
    store = AuditTrail(":memory:", clock)
    yield store
    store.close()


@pytest.fixture
def file_trail(workdir, clock):
    store = AuditTrail(workdir / "audit.sqlite", clock)
    for index in range(5):
        store.append(
            actor=f"user{index}",
            action="run.completed",
            entity_type="run",
            entity_id=f"RUN-{index}",
            payload={"index": index},
        )
    yield store
    store.close()


class TestAppend:
    def test_genesis_record_is_written_on_creation(self, trail):
        assert trail.count() == 1
        genesis = trail.get(1)
        assert genesis.action == "audit.initialised"
        assert genesis.prev_hash == GENESIS_HASH

    def test_append_returns_the_record(self, trail):
        record = trail.append("qa_lead", "document.signed", "document", "DOC-1", {"a": 1})
        assert record.seq == 2
        assert record.actor == "qa_lead"
        assert record.payload == {"a": 1}
        assert len(record.row_hash) == 64

    def test_sequence_is_gap_free_and_monotonic(self, trail):
        for index in range(10):
            trail.append("u", "a", "e", str(index))
        assert [r.seq for r in trail.records()] == list(range(1, 12))

    def test_each_record_links_to_the_previous(self, trail):
        first = trail.append("u", "a", "e", "1")
        second = trail.append("u", "a", "e", "2")
        assert second.prev_hash == first.row_hash

    def test_timestamps_come_from_the_injected_clock(self, workdir):
        store = AuditTrail(workdir / "a.sqlite", FrozenClock("2026-03-01T00:00:00Z", step=1))
        record = store.append("u", "a", "e", "1")
        assert record.ts == "2026-03-01T00:00:01Z"
        store.close()

    def test_actor_is_required(self, trail):
        with pytest.raises(AuditError, match="actor"):
            trail.append("", "action", "entity", "1")

    def test_action_is_required(self, trail):
        with pytest.raises(AuditError, match="action"):
            trail.append("actor", "", "entity", "1")

    def test_batch_append(self, trail):
        records = trail.append_many(
            [
                {"actor": "a", "action": "x", "entity_type": "e", "entity_id": "1"},
                {"actor": "b", "action": "y", "entity_type": "e", "entity_id": "2"},
            ]
        )
        assert [r.seq for r in records] == [2, 3]
        assert trail.verify().ok


class TestRedaction:
    @pytest.mark.parametrize(
        "key", ["password", "secret", "token", "api_key", "credential", "otp"]
    )
    def test_secret_values_never_reach_the_table(self, trail, key):
        trail.append("u", "sign", "document", "DOC-1", {key: "s3cr3t-value"})
        raw = trail._connection.execute("SELECT payload FROM audit_log WHERE seq = 2").fetchone()
        assert "s3cr3t-value" not in raw[0]
        assert "REDACTED" in raw[0]

    def test_redaction_reaches_nested_structures(self, trail):
        trail.append("u", "sign", "d", "1", {"components": [{"password": "abc123"}]})
        assert "abc123" not in trail.export_jsonl()

    def test_non_secret_values_survive(self, trail):
        record = trail.append("u", "sign", "d", "1", {"meaning": "approved", "user": "qa_lead"})
        assert record.payload["meaning"] == "approved"


class TestVerification:
    def test_clean_chain_verifies(self, file_trail):
        result = file_trail.verify()
        assert result.ok
        assert result.records_checked == 6
        assert result.chain_digest == file_trail.chain_digest()

    def test_verification_is_falsy_when_broken(self, file_trail, workdir):
        _tamper(workdir / "audit.sqlite", "UPDATE audit_log SET actor = 'mallory' WHERE seq = 3")
        assert not file_trail.verify()

    def test_mutated_payload_is_detected_at_that_record(self, file_trail, workdir):
        _tamper(
            workdir / "audit.sqlite",
            "UPDATE audit_log SET payload = '{\"index\":999}' WHERE seq = 4",
        )
        result = file_trail.verify()
        assert not result.ok
        assert result.first_bad_seq == 4
        assert "content altered" in result.reason

    def test_mutated_timestamp_is_detected(self, file_trail, workdir):
        _tamper(
            workdir / "audit.sqlite",
            "UPDATE audit_log SET ts = '2020-01-01T00:00:00Z' WHERE seq = 3",
        )
        result = file_trail.verify()
        assert not result.ok
        assert result.first_bad_seq == 3

    def test_mutated_actor_is_detected(self, file_trail, workdir):
        _tamper(workdir / "audit.sqlite", "UPDATE audit_log SET actor = 'mallory' WHERE seq = 2")
        assert file_trail.verify().first_bad_seq == 2

    def test_deleted_record_leaves_a_detectable_gap(self, file_trail, workdir):
        _tamper(workdir / "audit.sqlite", "DELETE FROM audit_log WHERE seq = 3")
        result = file_trail.verify()
        assert not result.ok
        assert result.first_bad_seq == 3
        assert "sequence gap" in result.reason

    def test_reparented_record_is_detected(self, file_trail, workdir):
        _tamper(
            workdir / "audit.sqlite",
            f"UPDATE audit_log SET prev_hash = '{'f' * 64}' WHERE seq = 5",
        )
        result = file_trail.verify()
        assert not result.ok
        assert result.first_bad_seq == 5
        assert "broken link" in result.reason

    def test_a_forged_record_with_a_recomputed_hash_still_breaks_the_chain(
        self, file_trail, workdir
    ):
        """The realistic attack: an attacker who understands the hash schema.

        Rewriting one record and its own digest is not enough, because every
        later record commits to the old digest. Only rewriting the entire
        remainder of the trail would hide it.
        """
        path = workdir / "audit.sqlite"
        connection = sqlite3.connect(path)
        row = connection.execute("SELECT * FROM audit_log WHERE seq = 3").fetchone()
        prev_hash = row[8]
        forged_payload = {"index": 999}
        forged_hash = compute_row_hash(
            3, row[1], "mallory", row[3], row[4], row[5], forged_payload, row[7], prev_hash
        )
        connection.execute("DROP TRIGGER audit_log_no_update")
        connection.execute(
            "UPDATE audit_log SET actor = ?, payload = ?, row_hash = ? WHERE seq = 3",
            ("mallory", json.dumps(forged_payload, separators=(",", ":")), forged_hash),
        )
        connection.commit()
        connection.close()

        result = file_trail.verify()
        assert not result.ok
        assert result.first_bad_seq == 4
        assert "broken link" in result.reason

    def test_empty_trail_digest_is_the_genesis_hash(self, workdir):
        store = AuditTrail(workdir / "x.sqlite")
        store._connection.execute("DROP TRIGGER audit_log_no_delete")
        store._connection.execute("DELETE FROM audit_log")
        assert store.chain_digest() == GENESIS_HASH
        store.close()


class TestAppendOnlyEnforcement:
    def test_direct_update_is_rejected_by_the_database(self, file_trail, workdir):
        connection = sqlite3.connect(workdir / "audit.sqlite")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_log SET actor = 'x' WHERE seq = 2")
        connection.close()

    def test_direct_delete_is_rejected_by_the_database(self, file_trail, workdir):
        connection = sqlite3.connect(workdir / "audit.sqlite")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_log WHERE seq = 2")
        connection.close()

    def test_the_trigger_is_a_guard_rail_and_the_chain_is_the_control(
        self, file_trail, workdir
    ):
        """Dropping the trigger defeats it; the chain still catches the change."""
        _tamper(workdir / "audit.sqlite", "UPDATE audit_log SET actor = 'x' WHERE seq = 2")
        assert not file_trail.verify().ok


class TestQuerying:
    def test_filter_by_actor(self, file_trail):
        assert len(file_trail.filter(actor="user3")) == 1

    def test_filter_by_entity(self, file_trail):
        assert len(file_trail.filter(entity_type="run")) == 5

    def test_filter_by_action(self, file_trail):
        assert len(file_trail.filter(action="run.completed")) == 5

    def test_filter_by_time_window(self, file_trail):
        records = file_trail.records()
        window = file_trail.filter(since=records[2].ts, until=records[3].ts)
        assert [r.seq for r in window] == [3, 4]

    def test_limit_and_offset(self, file_trail):
        assert [r.seq for r in file_trail.filter(limit=2, offset=1)] == [2, 3]

    def test_results_are_always_ordered_by_sequence(self, file_trail):
        seqs = [r.seq for r in file_trail.filter()]
        assert seqs == sorted(seqs)

    def test_get_and_last(self, file_trail):
        assert file_trail.get(2).entity_id == "RUN-0"
        assert file_trail.last().seq == 6
        assert file_trail.get(999) is None

    def test_len_and_iteration(self, file_trail):
        assert len(file_trail) == 6
        assert len(list(file_trail)) == 6


class TestExport:
    def test_jsonl_export_is_one_record_per_line(self, file_trail):
        lines = file_trail.export_jsonl().splitlines()
        assert len(lines) == 6
        assert json.loads(lines[0])["seq"] == 1

    def test_text_export_carries_the_chain_digest(self, file_trail):
        text = file_trail.export_text()
        assert file_trail.chain_digest() in text
        assert "Chain verified  : YES" in text

    def test_text_export_reports_a_broken_chain(self, file_trail, workdir):
        _tamper(workdir / "audit.sqlite", "UPDATE audit_log SET actor = 'x' WHERE seq = 2")
        assert "Chain verified  : NO" in file_trail.export_text()

    def test_text_export_explains_how_to_verify_independently(self, file_trail):
        assert "verified independently" in file_trail.export_text() or (
            "independently" in file_trail.export_text()
        )

    def test_long_payloads_are_truncated_in_the_readable_form(self, trail):
        trail.append("u", "a", "e", "1", {"blob": "x" * 5000})
        assert "..." in trail.export_text()


class TestConcurrency:
    def test_two_writers_do_not_collide(self, workdir):
        """Concurrent appends must not duplicate or skip a sequence number."""
        path = workdir / "concurrent.sqlite"
        AuditTrail(path).close()

        errors: list[Exception] = []

        def writer(name: str) -> None:
            try:
                store = AuditTrail(path)
                for index in range(20):
                    store.append(name, "action", "entity", f"{name}-{index}")
                store.close()
            except Exception as error:  # pragma: no cover - only on failure
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        store = AuditTrail(path)
        seqs = [r.seq for r in store.records()]
        assert seqs == list(range(1, len(seqs) + 1)), "sequence must be gap-free"
        assert len(seqs) == 1 + 4 * 20
        assert store.verify().ok, "chain must survive concurrent writers"
        store.close()


class TestPersistence:
    def test_reopening_continues_the_chain(self, workdir, clock):
        path = workdir / "persist.sqlite"
        first = AuditTrail(path, clock)
        first.append("u", "a", "e", "1")
        digest = first.chain_digest()
        first.close()

        second = AuditTrail(path, clock)
        assert second.chain_digest() == digest
        record = second.append("u", "a", "e", "2")
        assert record.prev_hash == digest
        assert second.verify().ok
        second.close()

    def test_reopening_does_not_write_a_second_genesis(self, workdir):
        path = workdir / "persist2.sqlite"
        AuditTrail(path).close()
        AuditTrail(path).close()
        store = AuditTrail(path)
        assert store.filter(action="audit.initialised") and store.count() == 1
        store.close()

    def test_context_manager_closes(self, workdir):
        with AuditTrail(workdir / "ctx.sqlite") as store:
            store.append("u", "a", "e", "1")
        with pytest.raises(sqlite3.ProgrammingError):
            store.count()


def _tamper(path, sql: str) -> None:
    """Damage the database directly, bypassing every application control."""
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    connection.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    connection.execute(sql)
    connection.commit()
    connection.close()
