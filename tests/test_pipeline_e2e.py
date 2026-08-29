"""End-to-end tests: the product's central claims, asserted on real output.

Each test here corresponds to something ValKit says about itself. If one of
these fails, a claim in the README is false.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from valkit.audit import AuditTrail
from valkit.errors import IntegrityError
from valkit.esign import SignatureService, StaticIdentityStore
from valkit.evals import FixtureProvider, load_dataset
from valkit.models import DocumentType, SignatureMeaning, ValidationStatus
from valkit.pipeline import ValidationPipeline
from valkit.util import FrozenClock, sha256_text
from valkit.vault import EvidenceVault

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "valkit.yaml"
GOLDEN = ROOT / "examples" / "datasets" / "rave_als_golden.jsonl"

PASSWORDS = {"qa_lead": "pw-qa-lead", "csv_lead": "pw-csv-lead"}
NAMES = {"qa_lead": "Dana Okafor", "csv_lead": "Marek Nowak"}


def build(tmp_path: Path, *, start: str = "2026-01-01T09:00:00Z"):
    """Assemble a pipeline with everything in a temporary workspace."""
    clock = FrozenClock(start, step=1.0)
    identities = StaticIdentityStore(clock)
    for user_id, password in PASSWORDS.items():
        identities.add(user_id, NAMES[user_id], password, roles=[user_id])
    audit = AuditTrail(tmp_path / "audit.sqlite", clock)
    vault = EvidenceVault(tmp_path / "vault", clock)
    signatures = SignatureService(identities, clock, audit)
    dataset = load_dataset(str(GOLDEN))
    pipeline = ValidationPipeline(
        provider=FixtureProvider.from_dataset(dataset, model="fixture/rave-als"),
        vault=vault,
        audit=audit,
        signatures=signatures,
        clock=clock,
        base_dir=ROOT,
    )
    return pipeline, audit, vault, signatures


def sign_everything(pipeline, signatures):
    """Author as the CSV lead, approve as the QA lead."""
    sessions = {
        user: signatures.open_session(user, {"user_id": user, "password": PASSWORDS[user]})
        for user in PASSWORDS
    }
    used: set[str] = set()

    def credentials(user: str) -> dict[str, str]:
        if user in used:
            return {"password": PASSWORDS[user]}
        used.add(user)
        return {"user_id": user, "password": PASSWORDS[user]}

    for document in list(pipeline.record.documents):
        pipeline.sign(
            document.doc_id, "csv_lead", SignatureMeaning.AUTHORED,
            credentials("csv_lead"), sessions["csv_lead"],
        )
        for approver in pipeline.record.spec.signoff.approvers:
            pipeline.sign(
                document.doc_id, approver, SignatureMeaning.APPROVED,
                credentials(approver), sessions[approver],
            )


@pytest.fixture(scope="module")
def validated(tmp_path_factory):
    """A fully signed, validated package, built once."""
    tmp_path = tmp_path_factory.mktemp("validated")
    pipeline, audit, vault, signatures = build(tmp_path)
    pipeline.ingest_spec(SPEC)
    pipeline.assess_and_derive()
    pipeline.load_datasets()
    pipeline.run_evals()
    pipeline.execute_tests()
    pipeline.generate_docs()
    sign_everything(pipeline, signatures)
    pipeline.seal()
    record = pipeline.finalise()
    return {
        "pipeline": pipeline,
        "record": record,
        "audit": audit,
        "vault": vault,
        "signatures": signatures,
        "path": tmp_path,
    }


class TestTheCentralClaim:
    def test_a_signed_package_reaches_validated(self, validated):
        assert validated["record"].status is ValidationStatus.VALIDATED

    def test_the_package_contains_every_document(self, validated):
        types = {d.doc_type for d in validated["record"].documents}
        for expected in (
            DocumentType.URS,
            DocumentType.FRS,
            DocumentType.RISK_ASSESSMENT,
            DocumentType.VALIDATION_PLAN,
            DocumentType.CREDIBILITY_REPORT,
            DocumentType.OQ_REPORT,
            DocumentType.RTM,
            DocumentType.VSR,
            DocumentType.TOOL_QUALIFICATION,
        ):
            assert expected in types

    def test_every_readiness_condition_was_satisfied(self, validated):
        readiness = validated["pipeline"].readiness()
        assert readiness.ready
        assert not readiness.blockers
        assert len(readiness.satisfied) >= 6


class TestNumbersTraceEndToEnd:
    def test_the_oq_report_states_the_numbers_the_run_produced(self, validated):
        """Trace one metric from sample scores to the rendered sentence."""
        run = validated["pipeline"].run
        metric = run.metric("field_accuracy")

        # The metric agrees with the raw sample scores.
        scored = [s for s in run.samples if "exact_match" in s.scores and s.error is None]
        passed = [s for s in scored if s.scores["exact_match"].passed]
        assert metric.n == len(scored)
        assert metric.k == len(passed)

        # And the report states exactly those numbers.
        report = next(
            d for d in validated["record"].documents if d.doc_type is DocumentType.OQ_REPORT
        )
        assert f"k={metric.k}/{metric.n}" in report.content
        assert f"{metric.lower_bound:.4f}" in report.content
        assert f"{metric.target:.4f}" in report.content

    def test_the_bound_is_the_one_the_statistics_module_computes(self, validated):
        from valkit.stats import clopper_pearson_lower

        metric = validated["pipeline"].run.metric("field_accuracy")
        assert metric.lower_bound == pytest.approx(
            clopper_pearson_lower(metric.k, metric.n, metric.confidence), abs=1e-12
        )

    def test_the_credibility_report_has_all_seven_steps(self, validated):
        import re

        document = next(
            d
            for d in validated["record"].documents
            if d.doc_type is DocumentType.CREDIBILITY_REPORT
        )
        steps = re.findall(r"^## Step (\d) — ", document.content, re.MULTILINE)
        assert [int(s) for s in steps] == [1, 2, 3, 4, 5, 6, 7]

    def test_the_rtm_coverage_claim_matches_the_graph(self, validated):
        coverage = validated["pipeline"].graph.coverage()
        document = next(
            d for d in validated["record"].documents if d.doc_type is DocumentType.RTM
        )
        assert (
            f"{coverage.critical_covered} of {coverage.critical_total} verified"
            in document.content
        )
        assert coverage.complete


class TestIntegrity:
    def test_every_document_digest_matches_its_content(self, validated):
        for document in validated["record"].documents:
            assert document.content_sha256 == sha256_text(document.content)

    def test_documents_are_stored_in_the_vault(self, validated):
        vault = validated["vault"]
        stored = {r.metadata.get("doc_id") for r in vault.records() if r.kind == "document"}
        for document in validated["record"].documents:
            assert document.doc_id in stored

    def test_the_stored_document_is_byte_identical_to_the_record(self, validated):
        vault = validated["vault"]
        document = validated["record"].documents[0]
        record = next(
            r
            for r in vault.records()
            if r.kind == "document" and r.metadata.get("doc_id") == document.doc_id
        )
        assert vault.get_text(record.evidence_id) == document.content

    def test_the_audit_chain_verifies(self, validated):
        assert validated["audit"].verify().ok

    def test_the_vault_verifies(self, validated):
        assert validated["vault"].verify().ok

    def test_tampering_with_an_audit_row_is_detected_at_that_row(self, tmp_path):
        import sqlite3

        pipeline, audit, _, _ = build(tmp_path)
        pipeline.ingest_spec(SPEC)
        pipeline.assess_and_derive()
        assert audit.verify().ok
        target = audit.count() - 1

        connection = sqlite3.connect(tmp_path / "audit.sqlite")
        connection.execute("DROP TRIGGER audit_log_no_update")
        connection.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = ?", (target,))
        connection.commit()
        connection.close()

        verification = audit.verify()
        assert not verification.ok
        assert verification.first_bad_seq == target

    def test_tampering_with_a_stored_object_is_detected_on_read(self, validated):
        import stat

        vault = validated["vault"]
        record = vault.records()[0]
        path = vault._object_path(record.evidence_id)
        original = path.read_bytes()
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"tampered")
        try:
            with pytest.raises(IntegrityError):
                vault.get_bytes(record.evidence_id)
        finally:
            path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            path.write_bytes(original)
            path.chmod(stat.S_IRUSR)

    def test_the_vault_refuses_to_delete_under_retention(self, validated):
        from valkit.errors import VaultError

        vault = validated["vault"]
        with pytest.raises(VaultError, match="under retention"):
            vault.delete(vault.records()[0].evidence_id)


class TestSignatures:
    def test_every_document_carries_valid_signatures(self, validated):
        service = validated["signatures"]
        for document in validated["record"].documents:
            assert service.verify_document(document).ok

    def test_altering_a_signed_document_invalidates_its_signature(self, validated):
        service = validated["signatures"]
        document = validated["record"].documents[0]
        altered = document.replace(content=document.content + "\ninserted line")
        result = service.verify_document(altered)
        assert not result.ok
        assert "changed since it was signed" in result.failures[0].reason

    def test_a_signature_cannot_be_moved_to_another_document(self, validated):
        service = validated["signatures"]
        first, second = validated["record"].documents[0], validated["record"].documents[1]
        transplanted = second.replace(signatures=first.signatures)
        assert not service.verify_document(transplanted).ok

    def test_the_author_did_not_approve_their_own_document(self, validated):
        for document in validated["record"].documents:
            authors = {
                s.signer_id for s in document.signatures if s.meaning is SignatureMeaning.AUTHORED
            }
            approvers = {
                s.signer_id for s in document.signatures if s.meaning is SignatureMeaning.APPROVED
            }
            assert not (authors & approvers)

    def test_no_credential_value_appears_anywhere_in_the_package(self, validated):
        surfaces = [d.content for d in validated["record"].documents]
        surfaces.append(validated["audit"].export_jsonl())
        surfaces.append(validated["audit"].export_text())
        for password in PASSWORDS.values():
            for surface in surfaces:
                assert password not in surface


class TestReproducibility:
    def test_two_runs_produce_byte_identical_documents(self, tmp_path_factory):
        """The reproducibility claim the whole package rests on."""
        digests = []
        for index in range(2):
            path = tmp_path_factory.mktemp(f"repro{index}")
            pipeline, _, _, signatures = build(path)
            pipeline.ingest_spec(SPEC)
            pipeline.assess_and_derive()
            pipeline.load_datasets()
            pipeline.run_evals(run_id="RUN-FIXED")
            pipeline.execute_tests()
            documents = pipeline.generate_docs()
            digests.append([d.content_sha256 for d in documents])
        assert digests[0] == digests[1]

    def test_the_harness_configuration_digest_is_stable(self, tmp_path_factory):
        configs = []
        for index in range(2):
            path = tmp_path_factory.mktemp(f"harness{index}")
            pipeline, _, _, _ = build(path)
            pipeline.ingest_spec(SPEC)
            pipeline.assess_and_derive()
            pipeline.load_datasets()
            configs.append(pipeline.run_evals(run_id="R").harness.config_sha256)
        assert configs[0] == configs[1]


class TestRefusals:
    """Each condition of the validated gate, forced to fail on its own."""

    def _base(self, tmp_path):
        pipeline, audit, vault, signatures = build(tmp_path)
        pipeline.ingest_spec(SPEC)
        pipeline.assess_and_derive()
        pipeline.load_datasets()
        return pipeline, audit, vault, signatures

    def test_unsigned_documents_block_validation(self, tmp_path):
        pipeline, _, _, _ = self._base(tmp_path)
        pipeline.run_evals()
        pipeline.execute_tests()
        pipeline.generate_docs()
        readiness = pipeline.readiness()
        assert not readiness.ready
        assert any("lack the required approvals" in b for b in readiness.blockers)
        assert pipeline.finalise().status is not ValidationStatus.VALIDATED

    def test_a_failing_metric_blocks_validation_and_names_it(self, tmp_path):
        pipeline, _, _, signatures = self._base(tmp_path)
        spec = pipeline.record.spec
        harder = spec.replace(
            acceptance=spec.acceptance.replace(
                metrics=[m.replace(target=0.999) for m in spec.acceptance.metrics]
            )
        )
        pipeline.record = pipeline.record.replace(spec=harder)
        pipeline.run_evals()
        pipeline.execute_tests()
        pipeline.generate_docs()
        readiness = pipeline.readiness()
        assert not readiness.ready
        assert any("Critical acceptance criteria not met" in b for b in readiness.blockers)
        assert any("field_accuracy" in b for b in readiness.blockers)

    def test_a_missing_test_blocks_on_coverage(self, tmp_path):
        pipeline, _, _, _ = self._base(tmp_path)
        pipeline.run_evals()
        pipeline.execute_tests()
        # Remove the OQ that uniquely verifies field_accuracy.
        pipeline.record = pipeline.record.replace(
            tests=[t for t in pipeline.record.tests if t.metric_name != "field_accuracy"]
        )
        pipeline.graph = pipeline._build_graph()
        readiness = pipeline.readiness()
        assert not readiness.ready
        assert any("coverage is incomplete" in b for b in readiness.blockers)

    def test_a_broken_audit_chain_blocks_validation(self, tmp_path):
        import sqlite3

        pipeline, audit, _, signatures = self._base(tmp_path)
        pipeline.run_evals()
        pipeline.execute_tests()
        pipeline.generate_docs()
        sign_everything(pipeline, signatures)
        assert pipeline.readiness().ready

        connection = sqlite3.connect(tmp_path / "audit.sqlite")
        connection.execute("DROP TRIGGER audit_log_no_update")
        connection.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2")
        connection.commit()
        connection.close()

        readiness = pipeline.readiness()
        assert not readiness.ready
        assert any("Audit chain verification failed" in b for b in readiness.blockers)

    def test_a_corrupted_evidence_object_blocks_validation(self, tmp_path):
        import stat

        pipeline, _, vault, signatures = self._base(tmp_path)
        pipeline.run_evals()
        pipeline.execute_tests()
        pipeline.generate_docs()
        sign_everything(pipeline, signatures)
        assert pipeline.readiness().ready

        path = vault._object_path(vault.records()[0].evidence_id)
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.write_bytes(b"corrupted")

        readiness = pipeline.readiness()
        assert not readiness.ready
        assert any("Evidence vault verification failed" in b for b in readiness.blockers)

    def test_failed_judge_calibration_blocks_validation(self, tmp_path):
        pipeline, _, _, signatures = self._base(tmp_path)
        spec = pipeline.record.spec
        strict = spec.replace(
            acceptance=spec.acceptance.replace(
                judge_calibration=spec.acceptance.judge_calibration.replace(
                    min_cohen_kappa=0.999
                )
            )
        )
        pipeline.record = pipeline.record.replace(spec=strict)
        pipeline.run_evals()
        pipeline.execute_tests()
        pipeline.generate_docs()
        readiness = pipeline.readiness()
        assert any("Judge calibration failed" in b for b in readiness.blockers)

    def test_a_pinned_dataset_that_changed_is_refused(self, tmp_path):
        from valkit.errors import DatasetError

        pipeline, _, _, _ = build(tmp_path)
        pipeline.ingest_spec(SPEC)
        spec = pipeline.record.spec
        wrong = spec.replace(
            datasets=spec.datasets.replace(
                golden_set=spec.datasets.golden_set.replace(sha256="f" * 64)
            )
        )
        pipeline.record = pipeline.record.replace(spec=wrong)
        pipeline.assess_and_derive()
        with pytest.raises(DatasetError, match="not the one the validation plan approved"):
            pipeline.load_datasets()


class TestDemoScript:
    def test_the_demo_runs_and_reaches_validated(self, tmp_path):
        """A broken demonstration is worse than a broken test."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "examples" / "demo.py"), "--output", str(tmp_path / "d")],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Status: VALIDATED" in result.stdout
        assert (tmp_path / "d" / "OQ_REPORT.md").exists()
        assert (tmp_path / "d" / "CREDIBILITY_REPORT.md").exists()
        assert (tmp_path / "d" / "audit-trail.txt").exists()

    def test_the_demo_is_reproducible(self, tmp_path):
        digests = []
        for index in range(2):
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "examples" / "demo.py"),
                    "--output",
                    str(tmp_path / f"run{index}"),
                ],
                capture_output=True,
                cwd=str(ROOT),
                timeout=300,
                check=True,
            )
            digests.append(
                sha256_text((tmp_path / f"run{index}" / "OQ_REPORT.md").read_text())
            )
        assert digests[0] == digests[1]
