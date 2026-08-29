"""The hash-chained, append-only audit trail.

This is the backbone of 21 CFR 11.10(e). What makes it more than a log table is
the chain: each record commits to a digest of the record before it, so the
trail is verifiable arithmetically rather than on the operator's assurance.
Altering one payload, deleting one row, or re-ordering two entries invalidates
every digest that follows, and :meth:`AuditTrail.verify` reports exactly where.

The hashed field set is fixed and documented so that a third party can
reimplement verification from this docstring alone:

    row_hash = SHA-256( canonical_json({
        "seq":         <integer>,
        "ts":          <ISO-8601 UTC, Zulu>,
        "actor":       <string>,
        "action":      <string>,
        "entity_type": <string>,
        "entity_id":   <string>,
        "payload":     <the payload object>,
        "reason":      <string or null>,
        "prev_hash":   <the previous row's row_hash>,
    }) )

where ``canonical_json`` sorts keys, omits insignificant whitespace and
preserves UTF-8 (see :func:`valkit.util.canonical_json`). The genesis record
uses a ``prev_hash`` of sixty-four zeros.

Two properties are worth stating plainly because they are what an auditor will
probe. The chain proves *internal* consistency: it detects any change made
after the fact by anyone who does not rewrite the entire remainder of the
trail. It does not prove that a rewrite of the whole trail never happened; only
publishing the chain digest externally does that, which is what
:meth:`chain_digest` is for.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import AuditError
from ..models import AuditRecord
from ..util import Clock, SystemClock, canonical_json, redact, sha256_text

__all__ = ["AuditTrail", "ChainVerification", "GENESIS_HASH"]

GENESIS_HASH = "0" * 64

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class ChainVerification:
    """The outcome of verifying the audit chain."""

    ok: bool
    records_checked: int
    first_bad_seq: int | None = None
    reason: str = ""
    chain_digest: str = ""

    def __bool__(self) -> bool:
        return self.ok


def compute_row_hash(
    seq: int,
    ts: str,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Any,
    reason: str | None,
    prev_hash: str,
) -> str:
    """The chain digest for one record. See the module docstring for the schema."""
    return sha256_text(
        canonical_json(
            {
                "seq": seq,
                "ts": ts,
                "actor": actor,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload,
                "reason": reason,
                "prev_hash": prev_hash,
            }
        )
    )


class AuditTrail:
    """An append-only, hash-chained audit trail backed by SQLite.

    ``path`` may be ``":memory:"`` for tests. Opening an existing trail does not
    re-verify it; call :meth:`verify` explicitly, which is what installation
    qualification does.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        clock: Clock | None = None,
        *,
        timeout: float = 30.0,
    ):
        self.path = str(path)
        self._clock = clock or SystemClock()
        # Serialises writers inside one process. Across processes the IMMEDIATE
        # transaction plus SQLite's own locking does the same job.
        self._lock = threading.Lock()
        if self.path != ":memory:":
            # Otherwise a missing directory surfaces as sqlite3's opaque
            # "unable to open database file".
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self._initialise()

    # -- lifecycle ---------------------------------------------------------

    def _initialise(self) -> None:
        self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.count() == 0:
            self._append_unlocked(
                actor="system",
                action="audit.initialised",
                entity_type="audit_log",
                entity_id="genesis",
                payload={"schema": "valkit.audit.v1"},
                reason="Audit trail created",
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AuditTrail":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def append(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: Any = None,
        reason: str | None = None,
    ) -> AuditRecord:
        """Append one record and return it.

        The payload passes through :func:`valkit.util.redact` before storage. An
        electronic signature component must never be capable of reaching this
        table, and the audit trail is the one place in the system from which
        nothing can later be removed.
        """
        if not actor:
            raise AuditError("an audit record must name the actor responsible for the action")
        if not action:
            raise AuditError("an audit record must name the action")
        with self._lock:
            return self._append_unlocked(actor, action, entity_type, entity_id, payload, reason)

    def _append_unlocked(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: Any,
        reason: str | None,
    ) -> AuditRecord:
        safe_payload = redact(payload if payload is not None else {})
        timestamp = self._clock.now_iso()

        try:
            # IMMEDIATE takes the write lock before the sequence is read, so two
            # concurrent writers cannot allocate the same seq or leave a hole.
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = (row["seq"] + 1) if row else 1
            prev_hash = row["row_hash"] if row else GENESIS_HASH

            row_hash = compute_row_hash(
                seq, timestamp, actor, action, entity_type, entity_id,
                safe_payload, reason, prev_hash,
            )
            self._connection.execute(
                "INSERT INTO audit_log "
                "(seq, ts, actor, action, entity_type, entity_id, payload, reason, "
                " prev_hash, row_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    timestamp,
                    actor,
                    action,
                    entity_type,
                    entity_id,
                    canonical_json(safe_payload),
                    reason,
                    prev_hash,
                    row_hash,
                ),
            )
            self._connection.execute("COMMIT")
        except sqlite3.Error as error:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AuditError(f"could not append to the audit trail: {error}") from error

        return AuditRecord(
            seq=seq,
            ts=timestamp,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=safe_payload if isinstance(safe_payload, dict) else {"value": safe_payload},
            prev_hash=prev_hash,
            row_hash=row_hash,
            reason=reason,
        )

    def append_many(self, events: Sequence[dict[str, Any]]) -> list[AuditRecord]:
        """Append a batch of related events under a single lock acquisition."""
        with self._lock:
            return [
                self._append_unlocked(
                    actor=event["actor"],
                    action=event["action"],
                    entity_type=event.get("entity_type", ""),
                    entity_id=event.get("entity_id", ""),
                    payload=event.get("payload"),
                    reason=event.get("reason"),
                )
                for event in events
            ]

    # -- reading -----------------------------------------------------------

    @staticmethod
    def _to_record(row: sqlite3.Row) -> AuditRecord:
        payload = json.loads(row["payload"])
        return AuditRecord(
            seq=row["seq"],
            ts=row["ts"],
            actor=row["actor"],
            action=row["action"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            payload=payload if isinstance(payload, dict) else {"value": payload},
            prev_hash=row["prev_hash"],
            row_hash=row["row_hash"],
            reason=row["reason"],
        )

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])

    def get(self, seq: int) -> AuditRecord | None:
        row = self._connection.execute("SELECT * FROM audit_log WHERE seq = ?", (seq,)).fetchone()
        return self._to_record(row) if row else None

    def last(self) -> AuditRecord | None:
        row = self._connection.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return self._to_record(row) if row else None

    def records(self) -> list[AuditRecord]:
        return self.filter()

    def filter(
        self,
        actor: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditRecord]:
        """Query the trail. Results are always ordered by sequence."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("actor", actor),
            ("action", action),
            ("entity_type", entity_type),
            ("entity_id", entity_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)

        sql = "SELECT * FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        return [self._to_record(row) for row in self._connection.execute(sql, params)]

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self.records())

    def __len__(self) -> int:
        return self.count()

    # -- verification ------------------------------------------------------

    def chain_digest(self) -> str:
        """The last row's digest: the single value that seals the trail.

        Recording this outside the system - in a report, a countersigned
        document, or a separate store - is what converts the chain from a check
        on tampering into a check a third party can perform.
        """
        last = self.last()
        return last.row_hash if last else GENESIS_HASH

    def verify(self) -> ChainVerification:
        """Re-derive every digest and check the chain end to end.

        Detects a mutated field, a deleted row, a re-parented link and an
        out-of-order sequence, and reports the first sequence number at which
        the trail stops being trustworthy.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        checked = 0
        last_hash = GENESIS_HASH

        for row in self._connection.execute("SELECT * FROM audit_log ORDER BY seq"):
            seq = row["seq"]
            if seq != expected_seq:
                return ChainVerification(
                    ok=False,
                    records_checked=checked,
                    first_bad_seq=expected_seq,
                    reason=(
                        f"sequence gap: expected record {expected_seq}, found {seq}. "
                        "A record has been removed from the trail."
                    ),
                    chain_digest=last_hash,
                )
            if row["prev_hash"] != expected_prev:
                return ChainVerification(
                    ok=False,
                    records_checked=checked,
                    first_bad_seq=seq,
                    reason=(
                        f"broken link at record {seq}: prev_hash does not match the digest "
                        "of the preceding record. The trail has been re-parented or an "
                        "earlier record altered."
                    ),
                    chain_digest=last_hash,
                )

            payload = json.loads(row["payload"])
            recomputed = compute_row_hash(
                seq,
                row["ts"],
                row["actor"],
                row["action"],
                row["entity_type"],
                row["entity_id"],
                payload,
                row["reason"],
                row["prev_hash"],
            )
            if recomputed != row["row_hash"]:
                return ChainVerification(
                    ok=False,
                    records_checked=checked,
                    first_bad_seq=seq,
                    reason=(
                        f"content altered at record {seq}: the stored digest does not match "
                        "the record's contents."
                    ),
                    chain_digest=last_hash,
                )

            expected_prev = row["row_hash"]
            last_hash = row["row_hash"]
            expected_seq += 1
            checked += 1

        return ChainVerification(
            ok=True, records_checked=checked, reason="chain intact", chain_digest=last_hash
        )

    # -- export ------------------------------------------------------------

    def export_jsonl(self, **filters: Any) -> str:
        """The electronic copy required by 21 CFR 11.10(b)."""
        return "\n".join(record.to_json() for record in self.filter(**filters))

    def export_text(self, **filters: Any) -> str:
        """The human-readable copy required by 21 CFR 11.10(b).

        The chain digest is included in the footer so that a printed copy
        remains verifiable: a reader can re-derive the chain from the electronic
        record and compare the final digest against the one on the page.
        """
        records = self.filter(**filters)
        verification = self.verify()

        lines = [
            "AUDIT TRAIL",
            "=" * 78,
            f"Source          : {self.path}",
            f"Records         : {len(records)} shown of {self.count()} total",
            f"Chain digest    : {self.chain_digest()}",
            f"Chain verified  : {'YES' if verification.ok else 'NO - ' + verification.reason}",
            "",
            "This is a computer-generated record. Each entry commits to a digest of the",
            "entry before it; altering or removing any entry invalidates every digest",
            "that follows. Verification can be repeated independently from the electronic",
            "record using the algorithm documented in valkit/audit/store.py.",
            "",
            "-" * 78,
        ]

        for record in records:
            lines.extend(
                [
                    f"[{record.seq:>6}] {record.ts}  {record.actor}",
                    f"         action : {record.action}",
                    f"         entity : {record.entity_type} {record.entity_id}",
                ]
            )
            if record.reason:
                lines.append(f"         reason : {record.reason}")
            if record.payload:
                rendered = canonical_json(record.payload)
                if len(rendered) > 400:
                    rendered = rendered[:397] + "..."
                lines.append(f"         detail : {rendered}")
            lines.append(f"         digest : {record.row_hash}")
            lines.append("")

        lines.append("-" * 78)
        lines.append(f"End of audit trail. Chain digest: {self.chain_digest()}")
        return "\n".join(lines)
