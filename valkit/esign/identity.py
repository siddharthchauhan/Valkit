"""Signer identities and credential verification.

21 CFR Part 11 subpart C governs who may sign and how their identity is
established. The parts that shape this module:

    11.100(a) Each electronic signature shall be unique to one individual and
      shall not be reused by, or reassigned to, anyone else.
    11.200(a)(1) Employ at least two distinct identification components such as
      an identification code and password.
    11.200(a)(2) Be used only by their genuine owners.
    11.300(a) Maintaining the uniqueness of each combined identification code
      and password, such that no two individuals have the same combination.
    11.300(b) Ensuring that identification code and password issuances are
      periodically checked, recalled, or revised (e.g., to cover such events as
      password aging).

The single most important rule in this file is that a credential value never
leaves it. :meth:`IdentityStore.verify_components` returns the *names* of the
components that were satisfied and nothing else. No function here returns,
logs, stores in plaintext, or embeds in an exception message the value of a
password or a second factor. The audit trail is permanent and the signature
manifest is rendered into documents, so a credential that reached either would
be exposed for the life of the record.

:class:`StaticIdentityStore` is a working in-memory implementation suitable for
tests, demonstrations and small deployments. A real deployment substitutes an
implementation backed by the customer's directory, and the protocol is narrow
precisely so that substitution is easy.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from ..errors import AuthorizationError, SignatureError
from ..util import Clock, SystemClock, parse_utc

__all__ = [
    "COMPONENT_USER_ID",
    "COMPONENT_PASSWORD",
    "COMPONENT_SECOND_FACTOR",
    "PERSONAL_COMPONENTS",
    "SignerIdentity",
    "IdentityStore",
    "StaticIdentityStore",
    "hash_password",
    "verify_password",
]

COMPONENT_USER_ID = "user_id"
COMPONENT_PASSWORD = "password"
COMPONENT_SECOND_FACTOR = "second_factor"

# Components that 11.200(a)(1)(i) describes as "only executable by, and
# designed to be used only by, the individual". An identification code is not
# one of them: it is typically a username, it appears in records and reports,
# and colleagues know it. Only a secret the individual holds qualifies, which
# is why a second signing within a session may be authorised by a password but
# never by a user id alone.
PERSONAL_COMPONENTS = frozenset({COMPONENT_PASSWORD, COMPONENT_SECOND_FACTOR})

_PBKDF2_ITERATIONS = 480_000
_SALT_BYTES = 16


def hash_password(password: str, salt: bytes | None = None, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Derive a storable verifier from a password.

    PBKDF2-HMAC-SHA256 from the standard library, so there is no additional
    supplier to assess. The iteration count is stored alongside the digest so
    that it can be raised over time without invalidating existing credentials.
    """
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored verifier in constant time."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), digest_hex)


@dataclass
class SignerIdentity:
    """One individual authorised to apply electronic signatures."""

    user_id: str
    printed_name: str
    roles: list[str] = field(default_factory=list)
    email: str = ""
    active: bool = True
    password_updated_at: str | None = None
    components: list[str] = field(
        default_factory=lambda: [COMPONENT_USER_ID, COMPONENT_PASSWORD]
    )
    title: str = ""

    def __post_init__(self) -> None:
        if not self.printed_name.strip():
            raise SignatureError(
                f"signer {self.user_id!r} has no printed name. 21 CFR 11.50(a)(1) requires "
                "the signed record to display the printed name of the signer, so an "
                "identity without one cannot sign."
            )
        if self.printed_name.strip() == self.user_id:
            raise SignatureError(
                f"signer {self.user_id!r} has a printed name identical to the "
                "identification code. The printed name must be the individual's actual "
                "name, since that is what 11.50(a)(1) requires the record to display."
            )

    @property
    def personal_components(self) -> set[str]:
        return {c for c in self.components if c in PERSONAL_COMPONENTS}


@runtime_checkable
class IdentityStore(Protocol):
    """The narrow interface the signature service depends on."""

    def get(self, user_id: str) -> SignerIdentity | None: ...

    def verify_components(self, user_id: str, components: dict[str, str]) -> set[str]: ...

    def required_components(self, user_id: str) -> set[str]: ...


class StaticIdentityStore:
    """An in-memory identity store with PBKDF2 password verification."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        password_max_age_days: int | None = 90,
        second_factor_verifier: Callable[[str, str], bool] | None = None,
    ):
        self._identities: dict[str, SignerIdentity] = {}
        self._passwords: dict[str, str] = {}
        self._clock = clock or SystemClock()
        self.password_max_age_days = password_max_age_days
        # Injectable so a real TOTP, push approval or SSO assertion can be
        # substituted without this module knowing anything about it.
        self._second_factor_verifier = second_factor_verifier

    def add(
        self,
        user_id: str,
        printed_name: str,
        password: str,
        *,
        roles: list[str] | None = None,
        email: str = "",
        components: list[str] | None = None,
        password_updated_at: str | None = None,
        title: str = "",
    ) -> SignerIdentity:
        """Register an individual.

        Enforces 11.100(a): an identification code already in use cannot be
        reassigned, because a signature must be attributable to exactly one
        individual for the life of the record.
        """
        if user_id in self._identities:
            raise SignatureError(
                f"identification code {user_id!r} is already assigned. 21 CFR 11.100(a) "
                "requires each electronic signature to be unique to one individual and "
                "never reused or reassigned."
            )
        if not password:
            raise SignatureError(f"signer {user_id!r} must have a password component")

        identity = SignerIdentity(
            user_id=user_id,
            printed_name=printed_name,
            roles=list(roles or []),
            email=email,
            components=list(components or [COMPONENT_USER_ID, COMPONENT_PASSWORD]),
            password_updated_at=password_updated_at or self._clock.now_iso(),
            title=title,
        )
        self._identities[user_id] = identity
        self._passwords[user_id] = hash_password(password)
        return identity

    def deactivate(self, user_id: str) -> None:
        identity = self._identities.get(user_id)
        if identity is not None:
            identity.active = False

    def set_password(self, user_id: str, password: str) -> None:
        if user_id not in self._identities:
            raise AuthorizationError(f"unknown signer {user_id!r}")
        self._passwords[user_id] = hash_password(password)
        self._identities[user_id].password_updated_at = self._clock.now_iso()

    def get(self, user_id: str) -> SignerIdentity | None:
        return self._identities.get(user_id)

    def required_components(self, user_id: str) -> set[str]:
        identity = self._identities.get(user_id)
        if identity is None:
            raise AuthorizationError(f"unknown signer {user_id!r}")
        return set(identity.components)

    def password_expired(self, user_id: str) -> bool:
        """21 CFR 11.300(b): credentials must be periodically revised."""
        if self.password_max_age_days is None:
            return False
        identity = self._identities.get(user_id)
        if identity is None or identity.password_updated_at is None:
            return False
        age = self._clock.now() - parse_utc(identity.password_updated_at)
        return age.days > self.password_max_age_days

    def verify_components(self, user_id: str, components: dict[str, str]) -> set[str]:
        """Return the names of the components that were satisfied.

        Never returns, logs or raises a component value. An unknown user and a
        wrong password are both reported as an authorisation failure without
        distinguishing which, so the store cannot be used to enumerate valid
        identification codes.
        """
        identity = self._identities.get(user_id)
        if identity is None or not identity.active:
            raise AuthorizationError(
                "the supplied credentials do not identify an active authorised signer"
            )
        if self.password_expired(user_id):
            raise AuthorizationError(
                f"the password for {user_id!r} is older than "
                f"{self.password_max_age_days} days and must be changed before signing "
                "(21 CFR 11.300(b))"
            )

        satisfied: set[str] = set()

        supplied_id = components.get(COMPONENT_USER_ID)
        if supplied_id is not None and hmac.compare_digest(supplied_id, user_id):
            satisfied.add(COMPONENT_USER_ID)

        supplied_password = components.get(COMPONENT_PASSWORD)
        if supplied_password is not None:
            if verify_password(supplied_password, self._passwords.get(user_id, "")):
                satisfied.add(COMPONENT_PASSWORD)
            else:
                raise AuthorizationError(
                    "the supplied credentials do not identify an active authorised signer"
                )

        supplied_factor = components.get(COMPONENT_SECOND_FACTOR)
        if supplied_factor is not None:
            if self._second_factor_verifier is None:
                raise SignatureError(
                    "a second factor was supplied but no verifier is configured on the "
                    "identity store"
                )
            if not self._second_factor_verifier(user_id, supplied_factor):
                raise AuthorizationError(
                    "the supplied credentials do not identify an active authorised signer"
                )
            satisfied.add(COMPONENT_SECOND_FACTOR)

        return satisfied

    def has_role(self, user_id: str, role: str) -> bool:
        identity = self._identities.get(user_id)
        return bool(identity and role in identity.roles)

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._identities

    def __len__(self) -> int:
        return len(self._identities)
