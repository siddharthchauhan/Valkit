"""21 CFR Part 11 subpart C electronic signatures.

:mod:`identity` establishes who may sign and verifies their credentials without
ever letting a credential value escape. :mod:`signatures` applies signatures,
binds each to the exact content it signed, and enforces the component rules of
11.200(a)(1).
"""

from __future__ import annotations

from .identity import (
    COMPONENT_PASSWORD,
    COMPONENT_SECOND_FACTOR,
    COMPONENT_USER_ID,
    PERSONAL_COMPONENTS,
    IdentityStore,
    SignerIdentity,
    StaticIdentityStore,
    hash_password,
    verify_password,
)
from .signatures import (
    DocumentVerification,
    SignatureService,
    SignatureVerification,
    SigningSession,
)

__all__ = [
    "SignatureService",
    "SigningSession",
    "SignatureVerification",
    "DocumentVerification",
    "IdentityStore",
    "StaticIdentityStore",
    "SignerIdentity",
    "hash_password",
    "verify_password",
    "COMPONENT_USER_ID",
    "COMPONENT_PASSWORD",
    "COMPONENT_SECOND_FACTOR",
    "PERSONAL_COMPONENTS",
]
