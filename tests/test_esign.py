"""Tests for Part 11 electronic signatures.

The regulation is specific, so these tests are specific. Each clause of
11.200(a)(1) is exercised at its boundary, the 11.70 record link is attacked by
transferring a signature between documents, and one test sweeps every surface a
credential could escape through.
"""

from __future__ import annotations

import pytest

from valkit.audit import AuditTrail
from valkit.errors import AuthorizationError, SignatureError
from valkit.esign.identity import (
    COMPONENT_PASSWORD,
    COMPONENT_SECOND_FACTOR,
    COMPONENT_USER_ID,
    SignerIdentity,
    StaticIdentityStore,
    hash_password,
    verify_password,
)
from valkit.esign.signatures import SignatureService
from valkit.models import DocumentStatus, SignatureMeaning, SignoffSpec
from valkit.testing import make_document
from valkit.util import FrozenClock

PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "second-signer-password"


@pytest.fixture
def store(clock):
    identities = StaticIdentityStore(clock)
    identities.add("qa_lead", "Dana Okafor", PASSWORD, roles=["qa"], title="QA Lead")
    identities.add("csv_lead", "Marek Nowak", OTHER_PASSWORD, roles=["csv"], title="CSV Lead")
    return identities


@pytest.fixture
def audit(clock):
    return AuditTrail(":memory:", clock)


@pytest.fixture
def service(store, clock, audit):
    return SignatureService(store, clock, audit)


@pytest.fixture
def document():
    return make_document()


def creds(user_id: str, password: str) -> dict[str, str]:
    return {COMPONENT_USER_ID: user_id, COMPONENT_PASSWORD: password}


class TestPasswordHashing:
    def test_round_trip(self):
        stored = hash_password(PASSWORD)
        assert verify_password(PASSWORD, stored)
        assert not verify_password("wrong", stored)

    def test_salted_so_equal_passwords_hash_differently(self):
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_plaintext_never_appears_in_the_verifier(self):
        assert PASSWORD not in hash_password(PASSWORD)

    def test_malformed_verifier_is_rejected_not_crashed(self):
        for bad in ("", "nonsense", "md5$1$aa$bb", "pbkdf2_sha256$x$y$z"):
            assert not verify_password(PASSWORD, bad)


class TestIdentity:
    def test_printed_name_is_required(self):
        with pytest.raises(SignatureError, match="printed name"):
            SignerIdentity(user_id="u", printed_name="  ")

    def test_printed_name_must_differ_from_the_identification_code(self):
        """11.50(a)(1) requires the individual's name, not their username."""
        with pytest.raises(SignatureError, match="actual name"):
            SignerIdentity(user_id="qa_lead", printed_name="qa_lead")

    def test_identification_codes_are_unique(self, store):
        """11.100(a): a code may not be reassigned."""
        with pytest.raises(SignatureError, match="11.100"):
            store.add("qa_lead", "Someone Else", "another-password")

    def test_unknown_user_is_refused_without_confirming_existence(self, store):
        with pytest.raises(AuthorizationError) as excinfo:
            store.verify_components("nobody", creds("nobody", PASSWORD))
        assert "nobody" not in str(excinfo.value)

    def test_wrong_password_gives_the_same_message_as_unknown_user(self, store):
        with pytest.raises(AuthorizationError) as wrong:
            store.verify_components("qa_lead", creds("qa_lead", "bad"))
        with pytest.raises(AuthorizationError) as unknown:
            store.verify_components("ghost", creds("ghost", "bad"))
        assert str(wrong.value) == str(unknown.value)

    def test_deactivated_user_cannot_authenticate(self, store):
        store.deactivate("qa_lead")
        with pytest.raises(AuthorizationError):
            store.verify_components("qa_lead", creds("qa_lead", PASSWORD))

    def test_component_verification_returns_names_only(self, store):
        satisfied = store.verify_components("qa_lead", creds("qa_lead", PASSWORD))
        assert satisfied == {COMPONENT_USER_ID, COMPONENT_PASSWORD}

    def test_password_aging_blocks_signing(self, clock):
        """11.300(b) requires credentials to be periodically revised."""
        identities = StaticIdentityStore(clock, password_max_age_days=90)
        identities.add(
            "old_user", "Old User", PASSWORD, password_updated_at="2020-01-01T00:00:00Z"
        )
        with pytest.raises(AuthorizationError, match="11.300"):
            identities.verify_components("old_user", creds("old_user", PASSWORD))

    def test_password_aging_can_be_disabled(self, clock):
        identities = StaticIdentityStore(clock, password_max_age_days=None)
        identities.add("u", "A User", PASSWORD, password_updated_at="2000-01-01T00:00:00Z")
        assert identities.verify_components("u", creds("u", PASSWORD))

    def test_second_factor_uses_the_injected_verifier(self, clock):
        identities = StaticIdentityStore(
            clock, second_factor_verifier=lambda user, code: code == "123456"
        )
        identities.add(
            "u", "A User", PASSWORD,
            components=[COMPONENT_USER_ID, COMPONENT_PASSWORD, COMPONENT_SECOND_FACTOR],
        )
        satisfied = identities.verify_components(
            "u", {**creds("u", PASSWORD), COMPONENT_SECOND_FACTOR: "123456"}
        )
        assert COMPONENT_SECOND_FACTOR in satisfied

        with pytest.raises(AuthorizationError):
            identities.verify_components(
                "u", {**creds("u", PASSWORD), COMPONENT_SECOND_FACTOR: "000000"}
            )


class TestSessionComponentRules:
    """21 CFR 11.200(a)(1), clause by clause."""

    def test_opening_a_session_requires_all_components(self, service):
        with pytest.raises(AuthorizationError, match="all signature components"):
            service.open_session("qa_lead", {COMPONENT_USER_ID: "qa_lead"})

    def test_first_signing_in_a_session_requires_all_components(self, service, document):
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        with pytest.raises(AuthorizationError, match=r"11\.200"):
            service.sign(
                document, "qa_lead", SignatureMeaning.APPROVED,
                {COMPONENT_PASSWORD: PASSWORD}, session,
            )

    def test_subsequent_signing_accepts_a_personal_component_alone(self, service, document):
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.sign(document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD), session)
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.REVIEWED,
            {COMPONENT_PASSWORD: PASSWORD}, session,
        )
        assert signature.components_used == [COMPONENT_PASSWORD]
        assert not signature.is_first_in_session

    def test_subsequent_signing_refuses_the_identification_code_alone(self, service, document):
        """The crux: a user id is not "only executable by" the individual."""
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.sign(document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD), session)
        with pytest.raises(AuthorizationError, match="not such a component"):
            service.sign(
                document, "qa_lead", SignatureMeaning.REVIEWED,
                {COMPONENT_USER_ID: "qa_lead"}, session,
            )

    def test_signing_with_no_session_requires_all_components(self, service, document):
        """11.200(a)(1)(ii)."""
        with pytest.raises(AuthorizationError, match="not part of a continuous period"):
            service.sign(
                document, "qa_lead", SignatureMeaning.APPROVED,
                {COMPONENT_PASSWORD: PASSWORD}, None,
            )

    def test_signing_with_no_session_succeeds_with_all_components(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert signature.is_first_in_session

    def test_expired_session_is_no_longer_continuous(self, store, document):
        """The 11.200(a)(1)(i) to (ii) boundary, tested at the exact moment."""
        clock = FrozenClock("2026-01-01T00:00:00Z", step=0)
        service = SignatureService(store, clock, idle_timeout_seconds=60)
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.sign(document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD), session)

        clock._t = clock._t.replace(minute=0, second=59)
        assert session.is_live(clock.now_iso())
        service.sign(document, "qa_lead", SignatureMeaning.REVIEWED, {COMPONENT_PASSWORD: PASSWORD}, session)

        clock._t = clock._t.replace(minute=2, second=0)
        assert not session.is_live(clock.now_iso())
        with pytest.raises(AuthorizationError, match="not part of a continuous period"):
            service.sign(
                document, "qa_lead", SignatureMeaning.APPROVED,
                {COMPONENT_PASSWORD: PASSWORD}, session,
            )

    def test_session_lifetime_also_ends_continuity(self, store, document):
        clock = FrozenClock("2026-01-01T00:00:00Z", step=0)
        service = SignatureService(store, clock, lifetime_seconds=120, idle_timeout_seconds=10_000)
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        clock._t = clock._t.replace(minute=5)
        assert not session.is_live(clock.now_iso())

    def test_closed_session_is_not_live(self, service, document):
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.close_session(session)
        with pytest.raises(AuthorizationError, match="not part of a continuous period"):
            service.sign(
                document, "qa_lead", SignatureMeaning.APPROVED,
                {COMPONENT_PASSWORD: PASSWORD}, session,
            )

    def test_a_session_cannot_be_shared(self, service, document):
        """11.200(a)(2): signatures are used only by their genuine owners."""
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        with pytest.raises(AuthorizationError, match="different individual"):
            service.sign(
                document, "csv_lead", SignatureMeaning.APPROVED,
                creds("csv_lead", OTHER_PASSWORD), session,
            )


class TestRecordLinkage:
    """21 CFR 11.70."""

    def test_signature_binds_to_the_document_content(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert service.verify_signature(document, signature).ok

    def test_altering_one_character_voids_the_signature(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        altered = document.replace(content=document.content.replace("Protocol", "Protocol "))
        result = service.verify_signature(altered, signature)
        assert not result.ok
        assert "changed since it was signed" in result.reason

    def test_a_signature_cannot_be_transferred_to_another_document(self, service, document):
        """The excision attack 11.70 exists to prevent."""
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        other = make_document(doc_id="DOC-OQ-0002", content=document.content)
        result = service.verify_signature(other, signature)
        assert not result.ok
        assert "cannot be transferred" in result.reason

    def test_forging_the_manifest_digest_is_detected(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        forged = signature.replace(meaning=SignatureMeaning.REJECTED)
        result = service.verify_signature(document, forged)
        assert not result.ok
        assert "manifest digest" in result.reason

    def test_altering_the_printed_name_is_detected(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        forged = signature.replace(printed_name="Someone Else")
        assert not service.verify_signature(document, forged).ok

    def test_signing_a_document_whose_digest_is_stale_is_refused(self, service, document):
        tampered = document.replace(content=document.content + "\nappended after generation")
        with pytest.raises(SignatureError, match="altered since generation"):
            service.sign(tampered, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD))

    def test_verify_document_checks_every_signature(self, service, document):
        signed = service.apply(document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD))
        signed = service.apply(signed, "csv_lead", SignatureMeaning.APPROVED, creds("csv_lead", OTHER_PASSWORD))
        result = service.verify_document(signed)
        assert result.ok
        assert result.signatures_checked == 2

    def test_verify_document_reports_failures(self, service, document):
        signed = service.apply(document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD))
        altered = signed.replace(content=signed.content + "x")
        result = service.verify_document(altered)
        assert not result.ok
        assert len(result.failures) == 1


class TestSignatureManifest:
    """21 CFR 11.50."""

    def test_manifest_shows_the_three_required_items(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        manifest = service.manifest(signature)
        assert "Dana Okafor" in manifest
        assert signature.signed_at in manifest
        assert "Approved" in manifest

    def test_timestamp_is_utc(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert signature.signed_at.endswith("Z")

    def test_manifest_includes_the_record_link(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert signature.document_sha256 in service.manifest(signature)

    def test_manifest_block_for_an_unsigned_document(self, service, document):
        block = service.manifest_block(document)
        assert "unsigned" in block and "draft" in block

    def test_manifest_block_states_the_binding(self, service, document):
        signed = service.apply(document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD))
        block = service.manifest_block(signed)
        assert "21 CFR Part 11" in block
        assert "invalidates it" in block


class TestCredentialContainment:
    def test_no_credential_value_escapes_through_any_surface(self, service, document, audit):
        """One sweep over every place a password could plausibly leak."""
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD), session
        )
        surfaces = [
            signature.to_json(),
            service.manifest(signature),
            audit.export_jsonl(),
            audit.export_text(),
            repr(signature),
            repr(session),
        ]
        for surface in surfaces:
            assert PASSWORD not in surface

    def test_components_used_records_names_not_values(self, service, document):
        signature = service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert signature.components_used == [COMPONENT_PASSWORD, COMPONENT_USER_ID]

    def test_authorisation_failure_does_not_echo_the_attempt(self, service, document):
        with pytest.raises(AuthorizationError) as excinfo:
            service.sign(
                document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", "wrong-password")
            )
        assert "wrong-password" not in str(excinfo.value)


class TestAuditIntegration:
    def test_signing_is_recorded(self, service, document, audit):
        service.sign(document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD))
        records = audit.filter(action="document.signed")
        assert len(records) == 1
        assert records[0].actor == "qa_lead"
        assert records[0].payload["meaning"] == "approved"

    def test_session_lifecycle_is_recorded(self, service, audit):
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.close_session(session)
        assert audit.filter(action="signature.session_opened")
        assert audit.filter(action="signature.session_closed")

    def test_the_chain_stays_intact(self, service, document, audit):
        session = service.open_session("qa_lead", creds("qa_lead", PASSWORD))
        service.sign(document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD), session)
        assert audit.verify().ok

    def test_a_service_without_an_audit_trail_still_signs(self, store, clock, document):
        service = SignatureService(store, clock, audit=None)
        assert service.sign(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )


class TestDocumentWorkflow:
    def test_apply_does_not_mutate_the_input(self, service, document):
        signed = service.apply(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert document.signatures == []
        assert len(signed.signatures) == 1

    def test_approval_advances_status(self, service, document):
        signed = service.apply(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD)
        )
        assert signed.status is DocumentStatus.APPROVED

    def test_rejection_sets_rejected_status(self, service, document):
        signed = service.apply(
            document, "qa_lead", SignatureMeaning.REJECTED, creds("qa_lead", PASSWORD)
        )
        assert signed.status is DocumentStatus.REJECTED

    def test_status_reaches_approved_only_when_every_approver_has_signed(self, service, document):
        signoff = SignoffSpec(approvers=["qa_lead", "csv_lead"])
        partial = service.apply(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        assert partial.status is not DocumentStatus.APPROVED
        assert service.missing_approvers(partial, signoff) == ["csv_lead"]

        complete = service.apply(
            partial, "csv_lead", SignatureMeaning.APPROVED, creds("csv_lead", OTHER_PASSWORD),
            signoff=signoff,
        )
        assert complete.status is DocumentStatus.APPROVED
        assert service.required_signatures_met(complete, signoff)

    def test_author_may_not_approve_their_own_document(self, service, document):
        signoff = SignoffSpec(approvers=["qa_lead"], require_distinct_signers=True)
        authored = service.apply(
            document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        with pytest.raises(AuthorizationError, match="may not also approve"):
            service.apply(
                authored, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
                signoff=signoff,
            )

    def test_segregation_can_be_waived_explicitly(self, service, document):
        signoff = SignoffSpec(approvers=["qa_lead"], require_distinct_signers=False)
        authored = service.apply(
            document, "qa_lead", SignatureMeaning.AUTHORED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        approved = service.apply(
            authored, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        assert approved.status is DocumentStatus.APPROVED

    def test_a_non_approver_cannot_approve(self, service, document):
        signoff = SignoffSpec(approvers=["csv_lead"])
        with pytest.raises(AuthorizationError, match="not among the approvers"):
            service.apply(
                document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
                signoff=signoff,
            )

    def test_requirements_not_met_when_a_signature_is_invalid(self, service, document):
        signoff = SignoffSpec(approvers=["qa_lead"])
        signed = service.apply(
            document, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        altered = signed.replace(content=signed.content + "x")
        assert not service.required_signatures_met(altered, signoff)

    def test_rejection_blocks_approval_even_with_all_approvers(self, service, document):
        signoff = SignoffSpec(approvers=["qa_lead"])
        rejected = service.apply(
            document, "csv_lead", SignatureMeaning.REJECTED, creds("csv_lead", OTHER_PASSWORD)
        )
        approved = service.apply(
            rejected, "qa_lead", SignatureMeaning.APPROVED, creds("qa_lead", PASSWORD),
            signoff=signoff,
        )
        assert not service.required_signatures_met(approved, signoff)
