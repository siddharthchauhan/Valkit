"""Applying and verifying 21 CFR Part 11 electronic signatures.

The controlling text, quoted because the implementation follows it clause by
clause:

    11.50(a) Signed electronic records shall contain information associated
      with the signing that clearly indicates all of the following: (1) The
      printed name of the signer; (2) The date and time when the signature was
      executed; and (3) The meaning (such as review, approval, responsibility,
      or authorship) associated with the signature.
    11.50(b) The items identified in paragraphs (a)(1), (a)(2), and (a)(3) of
      this section shall be subject to the same controls as for electronic
      records and shall be included as part of any human readable form of the
      electronic record (such as electronic display or printout).
    11.70 Electronic signatures and handwritten signatures executed to
      electronic records shall be linked to their respective electronic records
      to ensure that the signatures cannot be excised, copied, or otherwise
      transferred to falsify an electronic record by ordinary means.
    11.200(a)(1)(i) When an individual executes a series of signings during a
      single, continuous period of controlled system access, the first signing
      shall be executed using all electronic signature components; subsequent
      signings shall be executed using at least one electronic signature
      component that is only executable by, and designed to be used only by,
      the individual.
    11.200(a)(1)(ii) When an individual executes one or more signings not
      performed during a single, continuous period of controlled system access,
      each signing shall be executed using all of the electronic signature
      components.

Two implementation choices are worth drawing out.

*The 11.70 link is a digest, not a foreign key.* A signature stores the SHA-256
of the exact content it was applied to. Altering the document by one character
invalidates verification, and a signature cannot be lifted from one document
and attached to another because verification also checks the document
identifier. A row-level association would satisfy neither test.

*Session expiry is a regulatory boundary, not a convenience.* "A single,
continuous period of controlled system access" is what distinguishes
11.200(a)(1)(i) from (a)(1)(ii). When a session lapses, the next signing
requires every component again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..audit.store import AuditTrail
from ..errors import AuthorizationError, SignatureError
from ..models import (
    Document,
    DocumentStatus,
    Signature,
    SignatureMeaning,
    SignoffSpec,
)
from ..util import Clock, SystemClock, canonical_json, parse_utc, sha256_text
from .identity import (
    COMPONENT_PASSWORD,
    COMPONENT_USER_ID,
    PERSONAL_COMPONENTS,
    IdentityStore,
    SignerIdentity,
)

__all__ = [
    "SigningSession",
    "SignatureService",
    "SignatureVerification",
    "DocumentVerification",
]

DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_SESSION_LIFETIME_SECONDS = 8 * 60 * 60


@dataclass
class SigningSession:
    """A single, continuous period of controlled system access.

    Opened with every component. Lapses on idle timeout or absolute lifetime,
    after which it is no longer continuous and the next signing needs all
    components again.
    """

    session_id: str
    user_id: str
    opened_at: str
    last_used_at: str
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS
    signing_count: int = 0
    closed: bool = False
    signatures: list[str] = field(default_factory=list)

    def is_live(self, now_iso: str) -> bool:
        if self.closed:
            return False
        now = parse_utc(now_iso)
        if (now - parse_utc(self.last_used_at)).total_seconds() > self.idle_timeout_seconds:
            return False
        if (now - parse_utc(self.opened_at)).total_seconds() > self.lifetime_seconds:
            return False
        return True

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class SignatureVerification:
    ok: bool
    signature_id: str
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class DocumentVerification:
    ok: bool
    document_id: str
    signatures_checked: int
    failures: list[SignatureVerification] = field(default_factory=list)
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class SignatureService:
    """Applies and verifies Part 11 electronic signatures."""

    def __init__(
        self,
        identity_store: IdentityStore,
        clock: Clock | None = None,
        audit: AuditTrail | None = None,
        *,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
        lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS,
    ):
        self._identities = identity_store
        self._clock = clock or SystemClock()
        self._audit = audit
        self._idle_timeout = idle_timeout_seconds
        self._lifetime = lifetime_seconds
        self._sessions: dict[str, SigningSession] = {}
        self._counter = 0

    # -- sessions ----------------------------------------------------------

    def open_session(self, user_id: str, components: dict[str, str]) -> SigningSession:
        """Begin a continuous period of controlled access.

        Requires every component the identity is configured with, per
        11.200(a)(1)(i): the first signing of a series is authenticated in full.
        """
        required = self._identities.required_components(user_id)
        satisfied = self._identities.verify_components(user_id, components)
        missing = required - satisfied
        if missing:
            raise AuthorizationError(
                f"opening a signing session requires all signature components; missing "
                f"{', '.join(sorted(missing))}"
            )

        now = self._clock.now_iso()
        self._counter += 1
        session = SigningSession(
            session_id=f"SESSION-{self._counter:06d}",
            user_id=user_id,
            opened_at=now,
            last_used_at=now,
            idle_timeout_seconds=self._idle_timeout,
            lifetime_seconds=self._lifetime,
        )
        self._sessions[session.session_id] = session
        self._record_audit(
            user_id, "signature.session_opened", "session", session.session_id,
            {"components_used": sorted(satisfied)},
        )
        return session

    def close_session(self, session: SigningSession) -> None:
        session.close()
        self._record_audit(
            session.user_id, "signature.session_closed", "session", session.session_id,
            {"signings": session.signing_count},
        )

    def session(self, session_id: str, user_id: str) -> SigningSession:
        """Look up an open session belonging to ``user_id``.

        The owner is a parameter rather than something the caller asserts
        afterwards, because a session identifier travelling over a network is a
        bearer token: without this check, knowing another individual's session
        identifier would be enough to sign in their name, which is exactly what
        11.200(a)(2) requires the system to prevent.
        """
        session = self._sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise AuthorizationError(
                f"no open signing session {session_id!r} belongs to {user_id!r}"
            )
        if not session.is_live(self._clock.now_iso()):
            raise AuthorizationError(
                f"signing session {session_id!r} has lapsed. 21 CFR 11.200(a)(1)(ii) "
                "requires all signature components outside a continuous session."
            )
        return session

    # -- signing -----------------------------------------------------------

    def sign(
        self,
        document: Document,
        signer_id: str,
        meaning: SignatureMeaning | str,
        components: dict[str, str],
        session: SigningSession | None = None,
        *,
        reason: str = "",
        role: str = "",
    ) -> Signature:
        """Apply an electronic signature to a document.

        The component requirement depends on whether this signing falls inside
        a live, continuous session:

        * no session, or a session that has lapsed: all components
          (11.200(a)(1)(ii));
        * the first signing of a live session: all components
          (11.200(a)(1)(i));
        * a subsequent signing in a live session: at least one component unique
          to the individual. A user id alone is refused, because an
          identification code is not "only executable by" its owner.
        """
        meaning = SignatureMeaning(meaning) if isinstance(meaning, str) else meaning

        identity = self._identities.get(signer_id)
        if identity is None or not getattr(identity, "active", True):
            raise AuthorizationError(
                "the supplied credentials do not identify an active authorised signer"
            )

        now = self._clock.now_iso()
        required_all = self._identities.required_components(signer_id)

        live = session is not None and session.is_live(now)
        if live and session.user_id != signer_id:
            raise AuthorizationError(
                "the signing session belongs to a different individual; a session may not "
                "be shared (21 CFR 11.200(a)(2))"
            )
        first_in_session = (not live) or session.signing_count == 0

        satisfied = self._identities.verify_components(signer_id, components)

        if first_in_session:
            missing = required_all - satisfied
            if missing:
                raise AuthorizationError(
                    "this signing is not part of a continuous period of controlled system "
                    "access, so all signature components are required; missing "
                    f"{', '.join(sorted(missing))} (21 CFR 11.200(a)(1))"
                )
        else:
            personal = satisfied & PERSONAL_COMPONENTS
            if not personal:
                raise AuthorizationError(
                    "a subsequent signing within a session requires at least one component "
                    "that is only executable by the individual. An identification code is "
                    "not such a component: supply the password or second factor "
                    "(21 CFR 11.200(a)(1)(i))."
                )

        signature = self._build_signature(
            document=document,
            identity=identity,
            meaning=meaning,
            satisfied=satisfied,
            session=session if live else None,
            first_in_session=first_in_session,
            now=now,
            reason=reason,
            role=role,
        )

        # The audit record is written before the signature is returned, and a
        # failure to write it aborts the signing. An unlogged signature would
        # be a record with no attributable origin, which is worse than a
        # refused signature.
        self._record_audit(
            signer_id,
            "document.signed",
            "document",
            document.doc_id,
            {
                "signature_id": signature.signature_id,
                "meaning": meaning.value,
                "document_sha256": signature.document_sha256,
                "components_used": signature.components_used,
                "manifest_sha256": signature.manifest_sha256,
            },
            reason=reason or None,
        )

        if live and session is not None:
            session.signing_count += 1
            session.last_used_at = now
            session.signatures.append(signature.signature_id)

        return signature

    def _build_signature(
        self,
        document: Document,
        identity: SignerIdentity,
        meaning: SignatureMeaning,
        satisfied: set[str],
        session: SigningSession | None,
        first_in_session: bool,
        now: str,
        reason: str,
        role: str,
    ) -> Signature:
        content_digest = sha256_text(document.content)
        if document.content_sha256 and document.content_sha256 != content_digest:
            raise SignatureError(
                f"document {document.doc_id} does not match its recorded digest; it has "
                "been altered since generation and must not be signed"
            )

        self._counter += 1
        signature_id = f"SIG-{self._counter:06d}"

        manifest_body = {
            "signature_id": signature_id,
            "document_id": document.doc_id,
            "document_sha256": content_digest,
            "signer_id": identity.user_id,
            "printed_name": identity.printed_name,
            "meaning": meaning.value,
            "signed_at": now,
            "components_used": sorted(satisfied),
            "reason": reason,
            "role": role or (identity.roles[0] if identity.roles else ""),
        }
        manifest_digest = sha256_text(canonical_json(manifest_body))

        return Signature(
            signature_id=signature_id,
            document_id=document.doc_id,
            document_sha256=content_digest,
            signer_id=identity.user_id,
            printed_name=identity.printed_name,
            meaning=meaning,
            signed_at=now,
            components_used=sorted(satisfied),
            session_id=session.session_id if session else "",
            is_first_in_session=first_in_session,
            manifest_sha256=manifest_digest,
            reason=reason,
            role=role or (identity.roles[0] if identity.roles else ""),
        )

    # -- verification ------------------------------------------------------

    def verify_signature(self, document: Document, signature: Signature) -> SignatureVerification:
        """Check a signature against the document it claims to sign.

        Fails if the document has been altered since signing, and fails if the
        signature belongs to a different document. Together those two checks are
        the 11.70 requirement that a signature cannot be excised, copied or
        transferred.
        """
        if signature.document_id != document.doc_id:
            return SignatureVerification(
                False,
                signature.signature_id,
                f"signature was executed against document {signature.document_id}, not "
                f"{document.doc_id}. A signature cannot be transferred between records.",
            )

        actual = sha256_text(document.content)
        if actual != signature.document_sha256:
            return SignatureVerification(
                False,
                signature.signature_id,
                "the document content has changed since it was signed: it now hashes to "
                f"{actual[:16]}... but was signed as {signature.document_sha256[:16]}.... "
                "The signature is void.",
            )

        identity = self._identities.get(signature.signer_id)
        if identity is None:
            return SignatureVerification(
                False,
                signature.signature_id,
                f"signer {signature.signer_id} is not a known identity",
            )
        if identity.printed_name != signature.printed_name:
            return SignatureVerification(
                False,
                signature.signature_id,
                "the printed name on the signature does not match the signer's identity",
            )

        expected_manifest = sha256_text(
            canonical_json(
                {
                    "signature_id": signature.signature_id,
                    "document_id": signature.document_id,
                    "document_sha256": signature.document_sha256,
                    "signer_id": signature.signer_id,
                    "printed_name": signature.printed_name,
                    "meaning": signature.meaning.value,
                    "signed_at": signature.signed_at,
                    "components_used": sorted(signature.components_used),
                    "reason": signature.reason,
                    "role": signature.role,
                }
            )
        )
        if expected_manifest != signature.manifest_sha256:
            return SignatureVerification(
                False,
                signature.signature_id,
                "the signature manifest digest does not match its contents; the signature "
                "record has been altered",
            )

        return SignatureVerification(True, signature.signature_id, "signature valid")

    def verify_document(self, document: Document) -> DocumentVerification:
        """Verify every signature on a document."""
        failures = [
            result
            for result in (
                self.verify_signature(document, signature) for signature in document.signatures
            )
            if not result.ok
        ]
        return DocumentVerification(
            ok=not failures,
            document_id=document.doc_id,
            signatures_checked=len(document.signatures),
            failures=failures,
            reason=(
                "all signatures valid"
                if not failures
                else f"{len(failures)} of {len(document.signatures)} signatures failed"
            ),
        )

    # -- document workflow -------------------------------------------------

    def apply(
        self,
        document: Document,
        signer_id: str,
        meaning: SignatureMeaning | str,
        components: dict[str, str],
        session: SigningSession | None = None,
        *,
        signoff: SignoffSpec | None = None,
        reason: str = "",
        role: str = "",
    ) -> Document:
        """Sign a document and return a new document carrying the signature.

        The input is not mutated: a signed document is a distinct record from
        the draft it was made from.
        """
        meaning = SignatureMeaning(meaning) if isinstance(meaning, str) else meaning

        if signoff is not None:
            self._check_segregation(document, signer_id, meaning, signoff)
            if meaning is SignatureMeaning.APPROVED and signoff.approvers:
                if signer_id not in signoff.approvers:
                    raise AuthorizationError(
                        f"{signer_id!r} is not among the approvers named in the "
                        f"specification ({', '.join(signoff.approvers)})"
                    )

        signature = self.sign(
            document, signer_id, meaning, components, session, reason=reason, role=role
        )
        signatures = [*document.signatures, signature]
        status = document.status
        if meaning is SignatureMeaning.REJECTED:
            status = DocumentStatus.REJECTED
        elif signoff is not None and self._approvals_met(signatures, signoff):
            status = DocumentStatus.APPROVED
        elif meaning in (SignatureMeaning.REVIEWED, SignatureMeaning.APPROVED):
            status = (
                DocumentStatus.APPROVED
                if signoff is None and meaning is SignatureMeaning.APPROVED
                else DocumentStatus.IN_REVIEW
            )
        return document.replace(signatures=signatures, status=status)

    @staticmethod
    def _check_segregation(
        document: Document,
        signer_id: str,
        meaning: SignatureMeaning,
        signoff: SignoffSpec,
    ) -> None:
        """An individual may not approve what they authored.

        Segregation of duties is not itself a Part 11 clause; it is a quality
        system control that Part 11's attribution requirements make enforceable.
        Where the specification asks for it, it is enforced rather than assumed.
        """
        if not signoff.require_distinct_signers:
            return
        if meaning not in (SignatureMeaning.APPROVED, SignatureMeaning.REVIEWED):
            return
        authored_by = {
            signature.signer_id
            for signature in document.signatures
            if signature.meaning is SignatureMeaning.AUTHORED
        }
        if signer_id in authored_by:
            verb = {"approved": "approve", "reviewed": "review"}.get(
                meaning.value, meaning.value
            )
            raise AuthorizationError(
                f"{signer_id!r} authored this document and may not also {verb} it; "
                "the specification requires distinct signers"
            )

    @staticmethod
    def _approvals_met(signatures: list[Signature], signoff: SignoffSpec) -> bool:
        approved = {
            signature.signer_id
            for signature in signatures
            if signature.meaning is SignatureMeaning.APPROVED
        }
        rejected = any(s.meaning is SignatureMeaning.REJECTED for s in signatures)
        if rejected:
            return False
        return bool(signoff.approvers) and set(signoff.approvers) <= approved

    def required_signatures_met(self, document: Document, signoff: SignoffSpec) -> bool:
        """Whether every approval the specification demands is present and valid."""
        if not self.verify_document(document).ok:
            return False
        return self._approvals_met(list(document.signatures), signoff)

    def missing_approvers(self, document: Document, signoff: SignoffSpec) -> list[str]:
        approved = {
            signature.signer_id
            for signature in document.signatures
            if signature.meaning is SignatureMeaning.APPROVED
        }
        return sorted(set(signoff.approvers) - approved)

    # -- rendering ---------------------------------------------------------

    def manifest(self, signature: Signature) -> str:
        """The human-readable signature block required by 11.50(b).

        Displays the three items 11.50(a) requires — printed name, UTC date and
        time, and meaning — plus the document digest that binds the signature to
        its record. Rendered as Markdown so it can be appended verbatim to a
        generated document.
        """
        lines = [
            "| Electronic signature | |",
            "| --- | --- |",
            f"| Printed name | {signature.printed_name} |",
            f"| Identification code | {signature.signer_id} |",
            f"| Date and time (UTC) | {signature.signed_at} |",
            f"| Meaning | {signature.meaning.value.capitalize()} |",
        ]
        if signature.role:
            lines.append(f"| Role | {signature.role} |")
        if signature.reason:
            lines.append(f"| Reason | {signature.reason} |")
        lines.extend(
            [
                f"| Document | {signature.document_id} |",
                f"| Document digest (SHA-256) | `{signature.document_sha256}` |",
                f"| Signature identifier | {signature.signature_id} |",
                f"| Components used | {', '.join(signature.components_used)} |",
            ]
        )
        return "\n".join(lines)

    def manifest_block(self, document: Document) -> str:
        """Every signature on a document, as one human-readable section."""
        if not document.signatures:
            return (
                "### Approvals\n\nThis document is unsigned. It is a draft and carries no "
                "approval.\n"
            )
        parts = ["### Approvals\n"]
        for signature in document.signatures:
            parts.append(self.manifest(signature))
            parts.append("")
        parts.append(
            "Electronic signatures executed in accordance with 21 CFR Part 11. Each "
            "signature is bound to the document digest shown above; any alteration of the "
            "document invalidates it."
        )
        return "\n".join(parts)

    # -- internals ---------------------------------------------------------

    def _record_audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            reason=reason,
        )
