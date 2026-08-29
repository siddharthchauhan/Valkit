"""The ALCOA+ append-only, hash-chained audit trail.

Every action that touches a validation record passes through here. The chain is
what makes 21 CFR 11.10(e) verifiable rather than merely asserted: see
:mod:`valkit.audit.store` for the exact digest schema, which is documented so
that a third party can reimplement verification independently.
"""

from __future__ import annotations

from .store import GENESIS_HASH, AuditTrail, ChainVerification, compute_row_hash

__all__ = ["AuditTrail", "ChainVerification", "GENESIS_HASH", "compute_row_hash"]
