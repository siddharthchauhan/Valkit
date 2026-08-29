"""The HTTP surface.

Four rules shape what is and is not here.

**Records are append-only.** There is no PUT, PATCH or DELETE anywhere in this
module. A document cannot be edited, a signature cannot be withdrawn, an audit
record cannot be amended. Superseding a document means generating a new one that
names its predecessor, which is what a controlled document system does and what
11.10(e) requires of the trail underneath it.

**Every mutating request is attributable.** ``X-ValKit-Actor`` is required on
every POST, and it is what lands in the audit trail. A record whose actor is
"the API" is not an audit trail.

**Signing is a POST with the credential in the body, and nowhere else.** Never a
query parameter: query strings reach access logs, proxy logs, browser history
and referrer headers.

**Acceptance failure is not an HTTP error.** A run whose agent missed its target
is a successful request with ``passed: false``; the caller asked a question and
got an answer. Integrity failure is different and is reported as a server error,
because it means this service cannot vouch for what it stored.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response

from valkit.errors import ValKitError
from valkit.spec.derive import derive_all
from valkit.spec.loader import parse_spec

from .deps import Services, Validation, get_services
from .schemas import (
    ChangeControlRequest,
    ChangeControlResponse,
    DocumentSummary,
    DriftResponse,
    IngestSpecRequest,
    ReadinessModel,
    RegisterSignerRequest,
    RunSummary,
    SignatureResponse,
    SignerSummary,
    SignRequest,
    SpecSummary,
    StartValidationRequest,
    ValidationSummary,
    VerificationResponse,
)

__all__ = ["router", "health_router"]

router = APIRouter(prefix="/api/v1")
health_router = APIRouter()


def actor(
    x_valkit_actor: str = Header(
        ...,
        alias="X-ValKit-Actor",
        description=(
            "The individual on whose behalf the request is made. Recorded in the "
            "audit trail; required on every request that writes a record."
        ),
    ),
) -> str:
    if not x_valkit_actor.strip():
        raise HTTPException(status_code=400, detail="X-ValKit-Actor must not be empty")
    return x_valkit_actor.strip()


def services(request: Request) -> Services:
    return get_services(request)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@health_router.get("/healthz", tags=["health"])
def healthz(svc: Services = Depends(services)) -> dict[str, Any]:
    """Liveness. Deliberately checks nothing.

    A liveness probe that verified the audit chain would take the service out of
    the load balancer for a data-integrity problem, replacing it with an
    identical instance that has the same problem. Integrity belongs on
    ``/readyz`` and in the alarms, not here.
    """
    return {"status": "ok", "version": svc.settings.version, "detail": {}}


@health_router.get("/readyz", tags=["health"])
def readyz(response: Response, svc: Services = Depends(services)) -> dict[str, Any]:
    """Readiness: can this instance serve a request that produces evidence?"""
    detail: dict[str, Any] = {}
    ok = True

    chain = svc.audit.verify()
    detail["audit_chain"] = {"ok": bool(chain.ok), "reason": chain.reason}
    ok = ok and bool(chain.ok)

    try:
        svc.vault.records()
        detail["evidence_vault"] = {"ok": True, "reason": ""}
    except ValKitError as error:
        detail["evidence_vault"] = {"ok": False, "reason": str(error)}
        ok = False

    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unavailable", "version": svc.settings.version, "detail": detail}


# --------------------------------------------------------------------------
# Specifications
# --------------------------------------------------------------------------


@router.post("/specs", response_model=SpecSummary, status_code=201, tags=["specifications"])
def ingest_spec(
    body: IngestSpecRequest,
    who: str = Depends(actor),
    svc: Services = Depends(services),
) -> SpecSummary:
    """Load and validate a specification, and derive what follows from it."""
    result = parse_spec(body.yaml, "<api>", strict=body.strict)
    bundle = derive_all(result.spec)
    spec = result.spec

    with svc.lock:
        svc.specs[spec.ref] = spec
        svc.specs[spec.agent_id] = spec

    svc.audit.append(
        actor=who,
        action="spec.ingested",
        entity_type="agent",
        entity_id=spec.ref,
        payload={"spec_sha256": spec.source_sha256, "warnings": result.warnings},
    )

    return SpecSummary(
        ref=spec.ref,
        agent_id=spec.agent_id,
        version=spec.version,
        gamp_category=int(spec.gamp.category.value),
        risk_class=bundle.assessment.risk_class.value,
        derived_risk_class=bundle.assessment.derived_class.value,
        requirements=len(bundle.requirements),
        risks=len(bundle.risks),
        tests=len(bundle.tests),
        spec_sha256=spec.source_sha256,
        warnings=result.warnings,
    )


@router.get("/example-spec", tags=["specifications"])
def example_spec(svc: Services = Depends(services)) -> Response:
    """A specification for the console's "load the example" button.

    Prefers the repository's ``examples/valkit.yaml``, because that one is
    runnable: it names the deterministic fixture provider and a golden set on
    disk, so the button is followed by a validation that actually completes.
    The packaged reference specification names Bedrock and S3 and is the
    fallback when the API is not running from a checkout.
    """
    from pathlib import Path

    from valkit.testing import EXAMPLE_YAML

    root = Path(svc.base_dir) if svc.base_dir else Path.cwd()
    runnable = root / "examples" / "valkit.yaml"
    content = runnable.read_text() if runnable.is_file() else EXAMPLE_YAML
    return Response(content=content, media_type="text/yaml; charset=utf-8")


@router.get("/specs", response_model=list[str], tags=["specifications"])
def list_specs(svc: Services = Depends(services)) -> list[str]:
    return sorted({spec.ref for spec in svc.specs.values()})


@router.get("/specs/{ref}", response_model=SpecSummary, tags=["specifications"])
def get_spec(ref: str, svc: Services = Depends(services)) -> SpecSummary:
    spec = svc.require_spec(ref)
    bundle = derive_all(spec)
    return SpecSummary(
        ref=spec.ref,
        agent_id=spec.agent_id,
        version=spec.version,
        gamp_category=int(spec.gamp.category.value),
        risk_class=bundle.assessment.risk_class.value,
        derived_risk_class=bundle.assessment.derived_class.value,
        requirements=len(bundle.requirements),
        risks=len(bundle.risks),
        tests=len(bundle.tests),
        spec_sha256=spec.source_sha256,
        warnings=[],
    )


# --------------------------------------------------------------------------
# Validations
# --------------------------------------------------------------------------


@router.post(
    "/validations", response_model=ValidationSummary, status_code=201, tags=["validations"]
)
def start_validation(
    body: StartValidationRequest,
    who: str = Depends(actor),
    svc: Services = Depends(services),
) -> ValidationSummary:
    """Run every stage up to but not including signing.

    Signing is excluded on purpose. A pipeline that signed on the author's
    behalf would defeat the point of requiring a signature, so the response
    comes back with the approvals listed among the blockers and the caller has
    to apply them itself.
    """
    spec = svc.require_spec(body.spec_ref)

    with svc.lock:
        pipeline = svc.new_pipeline(spec)
        validation_id = svc.next_id("VAL")
        result = pipeline.run_all(spec, run_id=body.run_id)
        validation = Validation(
            validation_id=validation_id,
            agent_id=spec.agent_id,
            agent_version=spec.version,
            pipeline=pipeline,
            created_at=result.record.created_at,
            warnings=list(result.warnings),
        )
        svc.validations[validation_id] = validation

    svc.audit.append(
        actor=who,
        action="validation.executed",
        entity_type="validation",
        entity_id=validation_id,
        payload={
            "agent": spec.ref,
            "run_id": result.run.run_id if result.run else None,
            "ready": result.readiness.ready,
        },
    )
    if result.run is not None:
        svc.monitor.record(result.run)

    return _validation_summary(svc, validation)


@router.get("/validations", response_model=list[str], tags=["validations"])
def list_validations(svc: Services = Depends(services)) -> list[str]:
    return sorted(svc.validations)


@router.get(
    "/validations/{validation_id}", response_model=ValidationSummary, tags=["validations"]
)
def get_validation(validation_id: str, svc: Services = Depends(services)) -> ValidationSummary:
    return _validation_summary(svc, svc.require_validation(validation_id))


@router.get("/validations/{validation_id}/run", response_model=RunSummary, tags=["validations"])
def get_run(validation_id: str, svc: Services = Depends(services)) -> RunSummary:
    validation = svc.require_validation(validation_id)
    if validation.pipeline.run is None:
        raise HTTPException(status_code=404, detail="this validation has no evaluation run")
    return _run_summary(validation.pipeline.run)


@router.get("/validations/{validation_id}/rtm", tags=["validations"])
def get_rtm(validation_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    """The traceability matrix, and — more usefully — its gaps."""
    from valkit.trace.rtm import build_rtm

    validation = svc.require_validation(validation_id)
    graph = validation.pipeline.graph
    if graph is None:
        raise HTTPException(status_code=404, detail="this validation has no traceability graph")

    coverage = graph.coverage()
    validation_result = graph.validate()
    return {
        "rows": [
            {
                "requirement_id": row.requirement_id,
                "text": row.text,
                "kind": row.kind,
                "critical": row.critical,
                "risks": row.risks,
                "tests": row.tests,
                "executions": row.executions,
                "runs": row.runs,
                "evidence": row.evidence,
                "documents": row.documents,
                "verdict": row.verdict,
            }
            for row in build_rtm(graph)
        ],
        "coverage": {
            "requirements_total": coverage.requirements_total,
            "requirements_covered": coverage.requirements_covered,
            "critical_total": coverage.critical_total,
            "critical_covered": coverage.critical_covered,
            "risks_total": coverage.risks_total,
            "risks_mitigated": coverage.risks_mitigated,
            "tests_total": coverage.tests_total,
            "tests_executed": coverage.tests_executed,
            "critical_coverage": coverage.critical_coverage,
            "complete": coverage.complete,
        },
        "findings": [
            {
                "kind": f.kind,
                "severity": f.severity,
                "message": f.message,
                "blocking": f.blocking,
                "node_ids": f.node_ids,
            }
            for f in validation_result.findings
        ],
    }


@router.get("/validations/{validation_id}/documents", tags=["validations"])
def list_documents(
    validation_id: str, svc: Services = Depends(services)
) -> list[DocumentSummary]:
    validation = svc.require_validation(validation_id)
    return [_document_summary(svc, validation, d) for d in validation.documents]


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@router.get("/documents/{doc_id}", tags=["documents"])
def get_document(
    doc_id: str,
    format: str = Query("markdown", pattern="^(markdown|html|json)$"),
    svc: Services = Depends(services),
) -> Response:
    """Fetch a document.

    Markdown is the record; HTML is the same bytes rendered for a human, which
    is what 11.50(b) asks for. Neither is regenerated on the way out — a
    document whose content changed between generation and display would not
    match the digest its signature is bound to.
    """
    validation, document = svc.find_document(doc_id)

    if format == "markdown":
        return Response(content=document.content, media_type="text/markdown; charset=utf-8")
    if format == "html":
        block = svc.signatures.manifest_block(document) if document.signatures else ""
        rendered = (
            validation.pipeline.generator.render_signature_block(document, block)
            if block
            else document
        )
        return Response(
            content=validation.pipeline.generator.to_html(rendered),
            media_type="text/html; charset=utf-8",
        )

    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "doc_id": document.doc_id,
            "doc_type": document.doc_type.value,
            "title": document.title,
            "version": document.version,
            "status": document.status.value,
            "content": document.content,
            "content_sha256": document.content_sha256,
            "generated_at": document.generated_at,
            "template": document.template,
            "evidence_refs": document.evidence_refs,
            "signatures": [
                {
                    "signature_id": s.signature_id,
                    "printed_name": s.printed_name,
                    "meaning": s.meaning.value,
                    "signed_at": s.signed_at,
                    "components_used": s.components_used,
                }
                for s in document.signatures
            ],
        }
    )


@router.get("/documents/{doc_id}/verify", response_model=VerificationResponse, tags=["documents"])
def verify_document(doc_id: str, svc: Services = Depends(services)) -> VerificationResponse:
    """Check every signature on a document against the content it was bound to."""
    _, document = svc.find_document(doc_id)
    verification = svc.signatures.verify_document(document)
    return VerificationResponse(
        ok=verification.ok,
        reason=verification.reason,
        checked=verification.signatures_checked,
        detail={
            "failures": [
                {"signature_id": f.signature_id, "reason": f.reason}
                for f in verification.failures
            ]
        },
    )


@router.post(
    "/documents/{doc_id}/signatures",
    response_model=SignatureResponse,
    status_code=201,
    tags=["signatures"],
)
def sign_document(
    doc_id: str,
    body: SignRequest = Body(...),
    who: str = Depends(actor),
    svc: Services = Depends(services),
) -> SignatureResponse:
    """Apply a Part 11 signature.

    The components go to the signature service and nowhere else: they are not
    logged, not echoed, and not written to the audit trail, which records that a
    signing happened and which component *kinds* satisfied it.

    The actor header and the signer are checked against each other. Signing on
    another individual's behalf is precisely what 11.200(a)(2) prohibits, and an
    API that let the two differ would make it a one-line mistake.
    """
    if who != body.user:
        raise HTTPException(
            status_code=403,
            detail=(
                f"X-ValKit-Actor is {who!r} but the signature is claimed for {body.user!r}. "
                "21 CFR 11.200(a)(2) requires signatures to be used only by their genuine "
                "owners."
            ),
        )

    validation, document = svc.find_document(doc_id)
    session = (
        svc.signatures.session(body.session_id, body.user) if body.session_id else None
    )

    with svc.lock:
        signed = validation.pipeline.sign(
            doc_id, body.user, body.meaning, dict(body.components), session, reason=body.reason
        )

    signature = signed.signatures[-1]
    return SignatureResponse(
        signature_id=signature.signature_id,
        document_id=signature.document_id,
        document_sha256=signature.document_sha256,
        printed_name=signature.printed_name,
        signed_at=signature.signed_at,
        meaning=signature.meaning.value,
        components_used=signature.components_used,
        manifest=svc.signatures.manifest(signature),
    )


# --------------------------------------------------------------------------
# Signers
# --------------------------------------------------------------------------


@router.post("/signers", response_model=SignerSummary, status_code=201, tags=["signatures"])
def register_signer(
    body: RegisterSignerRequest,
    who: str = Depends(actor),
    svc: Services = Depends(services),
) -> SignerSummary:
    identity = svc.identities.add(
        body.user_id,
        body.printed_name,
        body.password,
        roles=body.roles,
        email=body.email,
        title=body.title,
    )
    svc.audit.append(
        actor=who,
        action="signer.registered",
        entity_type="signer",
        entity_id=identity.user_id,
        payload={"printed_name": identity.printed_name, "roles": identity.roles},
    )
    return SignerSummary(
        user_id=identity.user_id,
        printed_name=identity.printed_name,
        roles=identity.roles,
        active=identity.active,
        components=identity.components,
    )


# --------------------------------------------------------------------------
# Monitoring and change control
# --------------------------------------------------------------------------


@router.get("/agents/{agent_id}/drift", response_model=DriftResponse, tags=["monitoring"])
def get_drift(
    agent_id: str,
    window: int = Query(20, ge=3, le=500),
    svc: Services = Depends(services),
) -> DriftResponse:
    from valkit.models import MonitoringSpec

    points: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for metric in svc.monitor.store.metrics(agent_id):
        for point in svc.monitor.store.series(agent_id, metric)[-window:]:
            points.append(
                {
                    "metric": metric,
                    "observed_at": point.observed_at,
                    "value": point.value,
                    "n": point.n,
                    "run_id": point.run_id,
                }
            )
        alert = svc.monitor.evaluate(agent_id, metric, MonitoringSpec(window=window))
        if alert is not None:
            violations.extend(
                {
                    "metric": metric,
                    "alert_id": alert.alert_id,
                    "rule": v.rule,
                    "severity": v.severity.value,
                    "value": v.value,
                    "description": v.description,
                }
                for v in alert.violations
            )
    return DriftResponse(agent_id=agent_id, points=points, violations=violations)


@router.post(
    "/change-controls", response_model=ChangeControlResponse, status_code=201, tags=["change"]
)
def open_change_control(
    body: ChangeControlRequest,
    who: str = Depends(actor),
    svc: Services = Depends(services),
) -> ChangeControlResponse:
    spec = svc.specs.get(body.agent_id)
    record = svc.change_register.open(
        agent_id=body.agent_id,
        agent_version=body.version or (spec.version if spec else ""),
        trigger=body.trigger,
        reason=body.reason,
    )
    assessed = svc.change_register.assess_impact(record.cc_id, spec, metrics=body.metrics)
    svc.audit.append(
        actor=who,
        action="change_control.requested",
        entity_type="change_control",
        entity_id=assessed.cc_id,
        payload={"agent_id": assessed.agent_id, "trigger": assessed.trigger.value},
    )
    return _change_control_response(assessed)


@router.get("/change-controls", tags=["change"])
def list_change_controls(
    agent_id: str | None = Query(None),
    svc: Services = Depends(services),
) -> list[ChangeControlResponse]:
    records = svc.change_register.all()
    if agent_id:
        records = [r for r in records if r.agent_id == agent_id]
    return [_change_control_response(r) for r in records]


@router.get("/change-controls/{cc_id}", response_model=ChangeControlResponse, tags=["change"])
def get_change_control(cc_id: str, svc: Services = Depends(services)) -> ChangeControlResponse:
    return _change_control_response(svc.change_register.get(cc_id))


# --------------------------------------------------------------------------
# Audit trail and evidence
# --------------------------------------------------------------------------


@router.get("/audit", tags=["audit"])
def get_audit(
    actor_filter: str | None = Query(None, alias="actor"),
    action: str | None = Query(None),
    entity_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    svc: Services = Depends(services),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if actor_filter:
        filters["actor"] = actor_filter
    if action:
        filters["action"] = action
    if entity_id:
        filters["entity_id"] = entity_id

    records = svc.audit.filter(**filters) if filters else svc.audit.records()
    total = len(records)
    return {
        "total": total,
        "returned": min(total, limit),
        "chain_digest": svc.audit.chain_digest(),
        "records": [
            {
                "seq": r.seq,
                "ts": r.ts,
                "actor": r.actor,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "payload": r.payload,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
                "reason": r.reason,
            }
            for r in records[-limit:]
        ],
    }


@router.get("/audit/export", tags=["audit"])
def export_audit(
    format: str = Query("text", pattern="^(text|jsonl)$"),
    svc: Services = Depends(services),
) -> Response:
    """11.10(b): accurate and complete copies, human-readable and electronic."""
    if format == "text":
        return Response(content=svc.audit.export_text(), media_type="text/plain; charset=utf-8")
    return Response(content=svc.audit.export_jsonl(), media_type="application/x-ndjson")


@router.get("/audit/verify", response_model=VerificationResponse, tags=["audit"])
def verify_audit(response: Response, svc: Services = Depends(services)) -> VerificationResponse:
    """Re-derive the whole chain from its genesis record."""
    chain = svc.audit.verify()
    if not chain.ok:
        response.status_code = 500
    return VerificationResponse(
        ok=bool(chain.ok),
        reason=chain.reason,
        checked=chain.records_checked,
        detail={"first_bad_seq": chain.first_bad_seq, "chain_digest": chain.chain_digest},
    )


@router.get("/evidence/verify", response_model=VerificationResponse, tags=["audit"])
def verify_evidence(response: Response, svc: Services = Depends(services)) -> VerificationResponse:
    """Re-derive every stored object's digest from its bytes."""
    verification = svc.vault.verify()
    if not verification.ok:
        response.status_code = 500
    return VerificationResponse(
        ok=bool(verification.ok),
        reason=verification.reason,
        checked=verification.objects_checked,
        detail={
            "corrupted": verification.corrupted,
            "missing": verification.missing,
            "unindexed": verification.unindexed,
        },
    )


@router.get("/evidence", tags=["audit"])
def list_evidence(
    agent_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    svc: Services = Depends(services),
) -> dict[str, Any]:
    records = svc.vault.records()
    if agent_id:
        records = [r for r in records if r.agent_id == agent_id]
    return {
        "total": len(records),
        "records": [
            {
                "evidence_id": r.evidence_id,
                "kind": r.kind,
                "size_bytes": r.size_bytes,
                "stored_at": r.stored_at,
                "content_type": r.content_type,
                "retention_until": r.retention_until,
                "agent_id": r.agent_id,
                "run_id": r.run_id,
            }
            for r in records[:limit]
        ],
    }


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------


def _validation_summary(svc: Services, validation: Validation) -> ValidationSummary:
    pipeline = validation.pipeline
    record = pipeline.record
    readiness = pipeline.readiness()
    return ValidationSummary(
        validation_id=validation.validation_id,
        agent_id=validation.agent_id,
        agent_version=validation.agent_version,
        status=record.status.value if record else "draft",
        created_at=validation.created_at,
        validated_at=record.validated_at if record else None,
        readiness=ReadinessModel(
            ready=readiness.ready,
            blockers=readiness.blockers,
            satisfied=readiness.satisfied,
            conditions=readiness.conditions,
        ),
        run=_run_summary(pipeline.run) if pipeline.run else None,
        documents=[_document_summary(svc, validation, d) for d in validation.documents],
        skipped_documents=pipeline.generator.skipped(),
        warnings=validation.warnings,
    )


def _run_summary(run: Any) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        status=run.status.value,
        passed=run.passed,
        started_at=run.started_at,
        model=run.model,
        dataset_sha256=run.dataset_sha256,
        transcripts_ref=run.transcripts_ref,
        metrics=[
            {
                "name": m.name,
                "k": m.k,
                "n": m.n,
                "point_estimate": m.point_estimate,
                "lower_bound": m.lower_bound,
                "target": m.target,
                "confidence": m.confidence,
                "method": m.method.value,
                "passed": m.passed,
                "critical": m.critical,
                "errors": m.errors,
                "rationale": m.rationale,
            }
            for m in run.metrics
        ],
        calibration=(
            {
                "cohen_kappa": run.calibration.cohen_kappa,
                "min_required": run.calibration.min_required,
                "passed": run.calibration.passed,
                "agreement": run.calibration.percent_agreement,
                "n": run.calibration.n,
            }
            if run.calibration
            else None
        ),
    )


def _document_summary(svc: Services, validation: Validation, document: Any) -> DocumentSummary:
    record = validation.pipeline.record
    signoff = record.spec.signoff if record else None
    met = (
        svc.signatures.required_signatures_met(document, signoff)
        if signoff is not None
        else False
    )
    return DocumentSummary(
        doc_id=document.doc_id,
        doc_type=document.doc_type.value,
        title=document.title,
        version=document.version,
        status=document.status.value,
        content_sha256=document.content_sha256,
        generated_at=document.generated_at,
        signature_count=len(document.signatures),
        signatures_required_met=met,
    )


def _change_control_response(record: Any) -> ChangeControlResponse:
    return ChangeControlResponse(
        cc_id=record.cc_id,
        agent_id=record.agent_id,
        status=record.status.value,
        trigger=record.trigger.value,
        reason=record.reason,
        opened_at=record.opened_at,
        required_scope=list(record.required_scope),
        impact=list(record.impact),
    )
