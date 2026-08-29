"""Tests for the HTTP API.

The assertions that matter most are in ``TestCredentialContainment``. A password
that reaches an access log, a 422 body or the audit trail is a durable leak, and
the interesting case is the one that is easy to miss: FastAPI's default handler
echoes the input that failed validation, which for a signing request is the
credential itself.

Everything here runs offline against the deterministic fixture provider, on a
frozen clock, in a temporary workspace.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi", reason="the API extra is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from api.deps import build_services  # noqa: E402
from api.main import create_app  # noqa: E402
from api.settings import Settings, from_environment  # noqa: E402
from valkit.util import FrozenClock  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC_YAML = (ROOT / "examples" / "valkit.yaml").read_text()
PASSWORD = "a-strong-signing-password"
ACTOR = {"X-ValKit-Actor": "qa_lead"}


@pytest.fixture
def client(tmp_path):
    services = build_services(
        Settings(workspace=tmp_path / "workspace"),
        clock=FrozenClock("2026-01-01T00:00:00Z", step=1.0),
        base_dir=ROOT,
    )
    with TestClient(create_app(services=services)) as test_client:
        test_client.services = services
        yield test_client


@pytest.fixture
def ingested(client):
    response = client.post("/api/v1/specs", json={"yaml": SPEC_YAML}, headers=ACTOR)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def validated(client, ingested):
    response = client.post(
        "/api/v1/validations", json={"spec_ref": "rave-als-generator"}, headers=ACTOR
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def signer(client):
    response = client.post(
        "/api/v1/signers",
        json={
            "user_id": "qa_lead",
            "printed_name": "Dana Okafor",
            "password": PASSWORD,
            "roles": ["qa"],
        },
        headers=ACTOR,
    )
    assert response.status_code == 201
    return response.json()


class TestHealth:
    def test_liveness_checks_nothing(self, client):
        """A liveness probe that verified integrity would replace an instance
        with an identical one that has the same problem."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readiness_verifies_the_chain_and_the_vault(self, client):
        body = client.get("/readyz").json()
        assert body["status"] == "ok"
        assert body["detail"]["audit_chain"]["ok"]
        assert body["detail"]["evidence_vault"]["ok"]

    def test_readiness_fails_when_the_chain_is_broken(self, client, tmp_path):
        audit = client.services.audit
        audit.append(
            actor="qa_lead", action="test.event", entity_type="thing", entity_id="x"
        )
        # Reach past the API into the raw table, which is what an attacker with
        # database access would do.
        with audit._lock:
            audit._connection.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            audit._connection.execute("UPDATE audit_log SET actor = 'someone else' WHERE seq = 2")

        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["detail"]["audit_chain"]["ok"] is False


class TestSpecifications:
    def test_ingests_and_derives(self, ingested):
        assert ingested["agent_id"] == "rave-als-generator"
        assert ingested["gamp_category"] == 5
        assert ingested["requirements"] > 0
        assert ingested["risks"] > 0
        assert ingested["tests"] > 0
        assert len(ingested["spec_sha256"]) == 64

    def test_a_malformed_specification_is_a_422_with_the_path(self, client):
        response = client.post(
            "/api/v1/specs",
            json={"yaml": "apiVersion: valkit/v1\nkind: AgentValidation\n"},
            headers=ACTOR,
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error_type"] == "SpecError"
        assert body["path"] == "metadata"

    def test_unknown_keys_are_rejected_by_default(self, client):
        spec = SPEC_YAML + "\nnot_a_real_section:\n  x: 1\n"
        response = client.post("/api/v1/specs", json={"yaml": spec}, headers=ACTOR)
        assert response.status_code == 422

    def test_an_unknown_request_field_is_rejected(self, client):
        """A silently ignored field in a request that produces a signed
        document is a defect nobody sees until an audit."""
        response = client.post(
            "/api/v1/specs", json={"yaml": SPEC_YAML, "stict": True}, headers=ACTOR
        )
        assert response.status_code == 422

    def test_ingestion_is_recorded_against_the_actor(self, client, ingested):
        records = client.get("/api/v1/audit?action=spec.ingested").json()["records"]
        assert records
        assert records[-1]["actor"] == "qa_lead"

    def test_the_actor_header_is_required(self, client):
        assert client.post("/api/v1/specs", json={"yaml": SPEC_YAML}).status_code == 422

    def test_an_empty_actor_is_refused(self, client):
        response = client.post(
            "/api/v1/specs", json={"yaml": SPEC_YAML}, headers={"X-ValKit-Actor": "   "}
        )
        assert response.status_code == 400

    def test_the_example_specification_is_runnable(self, client):
        """The console's "load the example" button is followed by "run"."""
        example = client.get("/api/v1/example-spec").text
        assert "fixture/" in example
        assert client.post("/api/v1/specs", json={"yaml": example}, headers=ACTOR).status_code == 201


class TestValidation:
    def test_produces_the_whole_package(self, validated):
        assert len(validated["documents"]) >= 14
        types = {d["doc_type"] for d in validated["documents"]}
        assert {"URS", "OQ_REPORT", "CREDIBILITY_REPORT", "RTM", "VSR"} <= types
        assert validated["skipped_documents"] == {}

    def test_reports_the_bounds_not_the_observed_rate(self, validated):
        metrics = {m["name"]: m for m in validated["run"]["metrics"]}
        field_accuracy = metrics["field_accuracy"]
        assert field_accuracy["lower_bound"] < field_accuracy["point_estimate"]
        assert field_accuracy["method"] == "clopper_pearson_lower"

    def test_it_stops_short_of_signing(self, validated):
        """A pipeline that signed on the author's behalf would defeat the
        purpose of requiring a signature."""
        assert validated["readiness"]["ready"] is False
        assert any("approval" in blocker for blocker in validated["readiness"]["blockers"])
        assert all(d["signature_count"] == 0 for d in validated["documents"])

    def test_unscripted_steps_are_conditions_rather_than_blockers(self, validated):
        conditions = validated["readiness"]["conditions"]
        assert conditions
        assert any("live operation" in condition for condition in conditions)

    def test_every_condition_is_reported_not_just_the_first(self, validated):
        readiness = validated["readiness"]
        assert len(readiness["satisfied"]) >= 4

    def test_an_unknown_specification_is_a_404(self, client):
        response = client.post(
            "/api/v1/validations", json={"spec_ref": "nope"}, headers=ACTOR
        )
        assert response.status_code == 404

    def test_the_run_is_fetchable_on_its_own(self, client, validated):
        response = client.get(f"/api/v1/validations/{validated['validation_id']}/run")
        assert response.status_code == 200
        assert response.json()["run_id"] == validated["run"]["run_id"]

    def test_the_rtm_reports_gaps_as_prominently_as_coverage(self, client, validated):
        body = client.get(f"/api/v1/validations/{validated['validation_id']}/rtm").json()
        assert body["rows"]
        assert body["coverage"]["complete"] is True
        assert body["coverage"]["critical_covered"] == body["coverage"]["critical_total"]
        assert "findings" in body

    def test_the_run_feeds_the_monitoring_series(self, client, validated):
        body = client.get("/api/v1/agents/rave-als-generator/drift").json()
        assert body["points"]
        assert {p["metric"] for p in body["points"]} >= {"field_accuracy"}


class TestDocuments:
    def test_markdown_is_the_record(self, client, validated):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.get(f"/api/v1/documents/{doc_id}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text.startswith("#")

    def test_the_served_content_matches_the_digest(self, client, validated):
        from valkit.util import sha256_text

        summary = validated["documents"][0]
        content = client.get(f"/api/v1/documents/{summary['doc_id']}").text
        assert sha256_text(content) == summary["content_sha256"]

    def test_html_is_the_same_record_rendered(self, client, validated):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.get(f"/api/v1/documents/{doc_id}?format=html")
        assert response.status_code == 200
        assert "<html" in response.text.lower()

    def test_an_unknown_format_is_refused(self, client, validated):
        doc_id = validated["documents"][0]["doc_id"]
        assert client.get(f"/api/v1/documents/{doc_id}?format=pdf").status_code == 422

    def test_documents_cannot_be_modified(self, client, validated):
        """Records are append-only. There is no PUT, PATCH or DELETE anywhere."""
        doc_id = validated["documents"][0]["doc_id"]
        for method in ("put", "patch", "delete"):
            response = getattr(client, method)(f"/api/v1/documents/{doc_id}")
            assert response.status_code == 405, method

    def test_no_route_in_the_api_mutates_in_place(self, client):
        """Records are append-only, and this is where that is enforced.

        Read from the OpenAPI schema rather than from ``app.routes``: the
        routes are nested inside included routers, so a walk of the top level
        sees only the two health endpoints and would pass with a DELETE sitting
        on every other path.
        """
        schema = client.get("/openapi.json").json()
        methods = {
            method.upper()
            for operations in schema["paths"].values()
            for method in operations
        }
        assert methods, "no operations were found to check"
        assert len(schema["paths"]) > 15, schema["paths"].keys()
        assert methods <= {"GET", "POST"}, sorted(methods)


class TestSigning:
    def test_signs_a_document(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["printed_name"] == "Dana Okafor"
        assert body["meaning"] == "approved"
        # 11.50(a): printed name, date and time, and meaning, in human-readable form.
        assert "Dana Okafor" in body["manifest"]
        assert "Approved" in body["manifest"]
        assert body["document_sha256"] in body["manifest"]

    def test_the_actor_and_the_signer_must_be_the_same_person(
        self, client, validated, signer
    ):
        """11.200(a)(2): signatures used only by their genuine owners."""
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "csv_lead",
                "meaning": "approved",
                "components": {"user_id": "csv_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        assert response.status_code == 403
        assert "11.200(a)(2)" in response.json()["error"]

    def test_a_wrong_credential_is_refused(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": "wrong"},
            },
            headers=ACTOR,
        )
        assert response.status_code == 403

    def test_a_signature_does_not_transfer_between_documents(
        self, client, validated, signer
    ):
        """11.70: a signature cannot be excised, copied or transferred."""
        first, second = validated["documents"][0], validated["documents"][1]
        client.post(
            f"/api/v1/documents/{first['doc_id']}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        assert client.get(f"/api/v1/documents/{first['doc_id']}/verify").json()["ok"]
        second_verify = client.get(f"/api/v1/documents/{second['doc_id']}/verify").json()
        assert second_verify["checked"] == 0

    def test_signing_shows_up_in_the_package_state(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        refreshed = client.get(f"/api/v1/validations/{validated['validation_id']}").json()
        signed = next(d for d in refreshed["documents"] if d["doc_id"] == doc_id)
        assert signed["signature_count"] == 1
        assert signed["signatures_required_met"] is True

    def test_someone_who_is_not_an_approver_cannot_approve(self, client, validated):
        """The specification names who signs off. Registration is not authority."""
        client.post(
            "/api/v1/signers",
            json={
                "user_id": "passer_by",
                "printed_name": "Alex Mensah",
                "password": PASSWORD,
            },
            headers={"X-ValKit-Actor": "passer_by"},
        )
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "passer_by",
                "meaning": "approved",
                "components": {"user_id": "passer_by", "password": PASSWORD},
            },
            headers={"X-ValKit-Actor": "passer_by"},
        )
        assert response.status_code == 403

    def test_a_stolen_session_identifier_is_not_enough(self, client, validated, signer):
        """A session identifier travelling over a network is a bearer token."""
        session = client.services.signatures.open_session(
            "qa_lead", {"user_id": "qa_lead", "password": PASSWORD}
        )
        client.services.identities.add("csv_lead", "Sam Adeyemi", "another-password")
        doc_id = validated["documents"][0]["doc_id"]

        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "csv_lead",
                "meaning": "reviewed",
                "components": {"user_id": "csv_lead"},
                "session_id": session.session_id,
            },
            headers={"X-ValKit-Actor": "csv_lead"},
        )
        assert response.status_code == 403


class TestCredentialContainment:
    """A tool result, a log line and a 422 body are all durable."""

    def test_the_credential_is_not_in_the_response(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        assert PASSWORD not in response.text

    def test_the_credential_is_not_in_the_audit_trail(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": PASSWORD},
            },
            headers=ACTOR,
        )
        assert PASSWORD not in client.get("/api/v1/audit?limit=5000").text
        assert PASSWORD not in client.get("/api/v1/audit/export?format=jsonl").text
        assert PASSWORD not in client.get("/api/v1/audit/export?format=text").text

    def test_a_malformed_signing_request_does_not_echo_the_credential(
        self, client, validated, signer
    ):
        """FastAPI's default 422 returns the input that failed validation.

        For a signing request that input is the password, so the handler
        redacts before echoing.
        """
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={"user": "qa_lead", "components": {"password": PASSWORD}},
            headers=ACTOR,
        )
        assert response.status_code == 422
        assert PASSWORD not in response.text
        assert "REDACTED" in response.text

    def test_a_refused_credential_is_not_echoed(self, client, validated, signer):
        doc_id = validated["documents"][0]["doc_id"]
        response = client.post(
            f"/api/v1/documents/{doc_id}/signatures",
            json={
                "user": "qa_lead",
                "meaning": "approved",
                "components": {"user_id": "qa_lead", "password": "the-wrong-one"},
            },
            headers=ACTOR,
        )
        assert "the-wrong-one" not in response.text

    def test_registering_a_signer_does_not_echo_the_password(self, client):
        response = client.post(
            "/api/v1/signers",
            json={"user_id": "new_lead", "printed_name": "Ola Nwosu", "password": PASSWORD},
            headers=ACTOR,
        )
        assert response.status_code == 201
        assert PASSWORD not in response.text
        assert PASSWORD not in client.get("/api/v1/audit?limit=5000").text

    def test_no_route_takes_a_credential_as_a_query_parameter(self, client):
        """Query strings reach access logs, proxy logs and browser history."""
        schema = client.get("/openapi.json").json()
        offenders = []
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                for parameter in operation.get("parameters", []):
                    if parameter.get("in") in {"query", "path"} and any(
                        word in parameter["name"].lower()
                        for word in ("password", "secret", "token", "credential", "component")
                    ):
                        offenders.append(f"{method.upper()} {path}?{parameter['name']}")
        assert offenders == []


class TestIntegrity:
    def test_the_chain_and_the_vault_verify(self, client, validated):
        chain = client.get("/api/v1/audit/verify").json()
        vault = client.get("/api/v1/evidence/verify").json()
        assert chain["ok"] and chain["checked"] > 0
        assert vault["ok"] and vault["checked"] > 0

    def test_a_corrupted_object_is_a_server_error_not_a_client_one(
        self, client, validated, tmp_path
    ):
        """Integrity failure means this service cannot vouch for what it
        stored, which is not the caller's mistake."""
        vault = client.services.vault
        record = vault.records()[0]
        path = vault._object_path(record.evidence_id)
        path.chmod(0o644)
        path.write_bytes(b"tampered")

        response = client.get("/api/v1/evidence/verify")
        assert response.status_code == 500
        assert response.json()["ok"] is False

    def test_acceptance_failure_is_not_an_http_error(self, client):
        """"The agent missed its target" and "the evidence cannot be trusted"
        are different conversations."""
        spec = SPEC_YAML.replace("target: 0.85", "target: 0.999")
        client.post("/api/v1/specs", json={"yaml": spec}, headers=ACTOR)
        response = client.post(
            "/api/v1/validations", json={"spec_ref": "rave-als-generator"}, headers=ACTOR
        )
        assert response.status_code == 201
        metrics = {m["name"]: m for m in response.json()["run"]["metrics"]}
        assert metrics["field_accuracy"]["passed"] is False
        assert response.json()["readiness"]["ready"] is False

    def test_the_audit_export_is_human_readable_and_electronic(self, client, validated):
        """11.10(b): accurate and complete copies in both forms."""
        text = client.get("/api/v1/audit/export?format=text")
        jsonl = client.get("/api/v1/audit/export?format=jsonl")
        assert text.headers["content-type"].startswith("text/plain")
        assert jsonl.headers["content-type"].startswith("application/x-ndjson")
        assert len(jsonl.text.splitlines()) == client.get("/api/v1/audit").json()["total"]

    def test_evidence_is_listed_with_its_retention(self, client, validated):
        body = client.get("/api/v1/evidence").json()
        assert body["total"] > 0
        assert all(record["retention_until"] for record in body["records"])


class TestChangeControl:
    def test_opens_and_assesses(self, client, ingested):
        response = client.post(
            "/api/v1/change-controls",
            json={
                "agent_id": "rave-als-generator",
                "reason": "The primary model is being upgraded.",
                "trigger": "model_version",
            },
            headers=ACTOR,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["required_scope"]
        assert body["status"]

    def test_an_open_change_control_blocks_validated_status(self, client, ingested):
        client.post(
            "/api/v1/change-controls",
            json={
                "agent_id": "rave-als-generator",
                "reason": "The primary model is being upgraded.",
                "trigger": "model_version",
            },
            headers=ACTOR,
        )
        validation = client.post(
            "/api/v1/validations", json={"spec_ref": "rave-als-generator"}, headers=ACTOR
        ).json()
        assert any(
            "change control" in blocker for blocker in validation["readiness"]["blockers"]
        )

    def test_it_is_listed_and_fetchable(self, client, ingested):
        opened = client.post(
            "/api/v1/change-controls",
            json={"agent_id": "rave-als-generator", "reason": "Prompt revision."},
            headers=ACTOR,
        ).json()
        listed = client.get("/api/v1/change-controls?agent_id=rave-als-generator").json()
        assert [r["cc_id"] for r in listed] == [opened["cc_id"]]
        assert client.get(f"/api/v1/change-controls/{opened['cc_id']}").json() == opened


class TestErrorShape:
    def test_every_error_has_the_same_shape(self, client):
        """A client that has to branch on which shape came back gets it wrong."""
        responses = [
            client.get("/api/v1/documents/nope"),
            client.get("/api/v1/validations/nope"),
            client.post("/api/v1/specs", json={"yaml": "not: a: spec"}, headers=ACTOR),
        ]
        for response in responses:
            assert response.status_code >= 400
            body = response.json()
            assert "error" in body and "error_type" in body


class TestSettings:
    def test_reads_the_deployment_variables(self):
        settings = from_environment(
            {
                "VALKIT_EVIDENCE_BUCKET": "valkit-evidence",
                "VALKIT_RETENTION_YEARS": "10",
                "VALKIT_OBJECT_LOCK_MODE": "COMPLIANCE",
                "AWS_REGION": "eu-west-1",
            }
        )
        assert settings.uses_s3
        assert settings.retention_years == 10
        assert settings.region == "eu-west-1"

    def test_defaults_are_local_and_offline(self):
        settings = from_environment({})
        assert settings.uses_s3 is False
        assert settings.retention_years == 7

    def test_a_malformed_retention_falls_back_rather_than_failing_to_start(self):
        assert from_environment({"VALKIT_RETENTION_YEARS": "ten"}).retention_years == 7
