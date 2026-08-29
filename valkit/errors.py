"""Exception hierarchy for ValKit.

Errors are typed by the compliance concern they belong to so that callers —
and the API layer — can distinguish "your spec is wrong" from "the evidence
chain is broken", which are very different conversations to have with a QA
lead.
"""

from __future__ import annotations

__all__ = [
    "ValKitError",
    "SpecError",
    "DatasetError",
    "EvalError",
    "ProviderError",
    "AcceptanceError",
    "IntegrityError",
    "AuditError",
    "VaultError",
    "SignatureError",
    "AuthorizationError",
    "DocumentError",
    "TraceabilityError",
    "ChangeControlError",
]


class ValKitError(Exception):
    """Base class for every error raised by ValKit."""


class SpecError(ValKitError):
    """A ``valkit.yaml`` specification is missing a field or is inconsistent."""

    def __init__(self, message: str, *, path: str | None = None):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class DatasetError(ValKitError):
    """A golden set or red-team set could not be loaded or failed validation."""


class EvalError(ValKitError):
    """An evaluation run could not be executed."""


class ProviderError(EvalError):
    """A model provider failed (transport, auth, rate limit, bad response)."""


class AcceptanceError(ValKitError):
    """Acceptance criteria could not be evaluated as written."""


class IntegrityError(ValKitError):
    """A hash chain, stored object or signed record failed verification.

    This is the most serious error class in the system: it means recorded
    evidence cannot be trusted.
    """


class AuditError(IntegrityError):
    """The audit trail could not be written or its chain failed verification."""


class VaultError(IntegrityError):
    """An evidence-vault operation violated the retention or WORM policy."""


class SignatureError(ValKitError):
    """An electronic signature could not be applied or failed verification."""


class AuthorizationError(SignatureError):
    """The actor is not authorised for the requested signing action."""


class DocumentError(ValKitError):
    """A document could not be rendered from its template and evidence."""


class TraceabilityError(ValKitError):
    """The traceability graph is inconsistent (dangling or orphaned nodes)."""


class ChangeControlError(ValKitError):
    """A change-control record could not be opened, advanced or closed."""
