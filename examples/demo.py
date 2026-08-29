#!/usr/bin/env python3
"""End-to-end demonstration: valkit.yaml in, signed validation package out.

Runs entirely offline against a deterministic fixture provider, so it works on
a laptop with no credentials and produces the same output every time. That is
not only a convenience for the demonstration: a tool that generates evidence
has to be able to show that it computes the right acceptance decision from a
known set of outputs, and that is only possible when the outputs are known.

    python examples/demo.py [--output DIR]

What it does, in the order a validation actually happens:

  1. load and validate the specification
  2. assess model risk and derive requirements, risks and IQ/OQ/PQ tests
  3. load the qualification set and verify its pinned digest
  4. execute the acceptance battery and calibrate the judge
  5. compute acceptance as one-sided lower confidence bounds
  6. record which tests the run demonstrates, with deviations
  7. generate the document package
  8. apply Part 11 electronic signatures, by two distinct signers
  9. seal the evidence into a signable manifest
 10. decide whether validated status is reached, and say why not if not
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from valkit.audit import AuditTrail  # noqa: E402
from valkit.esign import SignatureService, StaticIdentityStore  # noqa: E402
from valkit.evals import FixtureProvider, load_dataset  # noqa: E402
from valkit.models import SignatureMeaning  # noqa: E402
from valkit.pipeline import ValidationPipeline  # noqa: E402
from valkit.util import FrozenClock  # noqa: E402
from valkit.vault import EvidenceVault  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "valkit.yaml"

# Demonstration credentials. A real deployment binds an identity store to the
# customer's directory; nothing here is a suggestion about how to hold secrets.
SIGNERS = {
    "qa_lead": ("Dana Okafor", "demo-password-qa", "Quality Assurance Lead"),
    "csv_lead": ("Marek Nowak", "demo-password-csv", "Computer System Validation Lead"),
}


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n{'─' * max(len(title), 60)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", default="out/demo", help="where to write the package")
    parser.add_argument("--keep", action="store_true", help="keep any existing workspace")
    args = parser.parse_args()

    output = Path(args.output)
    workspace = output / ".valkit"
    if output.exists() and not args.keep:
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    # A frozen clock makes the whole demonstration reproducible: run it twice
    # and every digest matches.
    clock = FrozenClock("2026-01-01T09:00:00Z", step=1.0)

    identities = StaticIdentityStore(clock)
    for user_id, (printed_name, password, title) in SIGNERS.items():
        identities.add(user_id, printed_name, password, roles=[user_id], title=title)

    audit = AuditTrail(workspace / "audit.sqlite", clock)
    vault = EvidenceVault(workspace / "vault", clock)
    signatures = SignatureService(identities, clock, audit)

    dataset = load_dataset(str(SPEC.parent / "datasets" / "rave_als_golden.jsonl"))
    pipeline = ValidationPipeline(
        provider=FixtureProvider.from_dataset(dataset, model="fixture/rave-als"),
        vault=vault,
        audit=audit,
        signatures=signatures,
        clock=clock,
        base_dir=ROOT,
    )

    rule("1. Specification")
    record = pipeline.ingest_spec(SPEC)
    print(f"  agent            {record.agent_id} v{record.agent_version}")
    print(f"  specification    {SPEC.relative_to(ROOT)}")
    print(f"  digest           {record.spec.source_sha256[:32]}…")

    rule("2. Risk assessment")
    bundle = pipeline.assess_and_derive()
    assessment = bundle.assessment
    print(f"  model influence  {assessment.model_influence.value}")
    print(f"  consequence      {assessment.decision_consequence.value}")
    print(f"  matrix result    {assessment.matrix_cell.value}")
    print(f"  applied class    {assessment.risk_class.value.upper()}")
    for line in assessment.escalations:
        print(f"    * {line[:96]}")
    print(f"  derived          {len(bundle.requirements)} requirements, "
          f"{len(bundle.risks)} risks, {len(bundle.tests)} tests")

    rule("3. Qualification data")
    data = pipeline.load_datasets()
    print(f"  cases            {len(data.samples)}")
    print(f"  digest verified  {data.sha256[:32]}…")
    for warning in pipeline.warnings:
        print(f"    ! {warning[:96]}")

    rule("4. Acceptance battery")
    run = pipeline.run_evals()
    print(f"  run              {run.run_id} ({run.status.value})")
    print(f"  harness digest   {run.harness.config_sha256[:32]}…")
    print()
    for metric in run.metrics:
        mark = "PASS" if metric.passed else "FAIL"
        advisory = " (advisory)" if not metric.critical else ""
        bound = f"{metric.lower_bound:.4f}" if metric.lower_bound is not None else "n/a"
        print(f"  [{mark}] {metric.name}{advisory}")
        print(f"         k={metric.k}/{metric.n}  p-hat={metric.point_estimate:.4f}  "
              f"bound={bound}  target={metric.target:.4f}")
    if run.calibration:
        mark = "PASS" if run.calibration.passed else "FAIL"
        print(f"\n  [{mark}] judge calibration  kappa={run.calibration.cohen_kappa:.3f} "
              f"over n={run.calibration.n} (required >= {run.calibration.min_required:.2f})")

    rule("5. Test execution and documents")
    executions = pipeline.execute_tests()
    deviations = sum(len(e.deviations) for e in executions)
    print(f"  executed         {len(executions)} tests, {deviations} deviation(s) recorded")
    documents = pipeline.generate_docs()
    print(f"  generated        {len(documents)} documents")

    rule("6. Electronic signatures")
    # The author of a document may not also approve it, so the two signers take
    # different roles: this is the segregation of duties the specification asks
    # for, enforced rather than assumed.
    author_session = signatures.open_session(
        "csv_lead", {"user_id": "csv_lead", "password": SIGNERS["csv_lead"][1]}
    )
    approver_session = signatures.open_session(
        "qa_lead", {"user_id": "qa_lead", "password": SIGNERS["qa_lead"][1]}
    )
    sessions = {"csv_lead": author_session, "qa_lead": approver_session}
    used: set[str] = set()

    def credentials(user_id: str) -> dict[str, str]:
        """All components for the first signing of a session, one thereafter.

        21 CFR 11.200(a)(1)(i): the first signing in a single, continuous
        period of controlled system access uses every component; subsequent
        signings use at least one component that only the individual can
        execute. An identification code is not such a component, so the
        password is what carries the later signings.
        """
        password = SIGNERS[user_id][1]
        if user_id in used:
            return {"password": password}
        used.add(user_id)
        return {"user_id": user_id, "password": password}

    for document in list(pipeline.record.documents):
        pipeline.sign(
            document.doc_id, "csv_lead", SignatureMeaning.AUTHORED,
            credentials("csv_lead"), author_session,
            reason="Prepared from the recorded evidence.",
        )
        for approver in pipeline.record.spec.signoff.approvers:
            pipeline.sign(
                document.doc_id, approver, SignatureMeaning.APPROVED,
                credentials(approver), sessions[approver],
                reason="Reviewed and approved for the stated context of use.",
            )
    signed = sum(len(d.signatures) for d in pipeline.record.documents)
    print(f"  applied          {signed} signatures across "
          f"{len(pipeline.record.documents)} documents")
    print("  first signing in each session used all components; subsequent")
    print("  signings used the password alone, per 21 CFR 11.200(a)(1)(i)")

    rule("7. Seal and decide")
    manifest = pipeline.seal()
    print(f"  manifest         {manifest.manifest_id} over {manifest.count} artefacts")
    print(f"  manifest digest  {manifest.manifest_sha256[:32]}…")
    final = pipeline.finalise()
    readiness = pipeline.readiness()

    print()
    for line in readiness.satisfied:
        print(f"  \033[32m+\033[0m {line}")
    for line in readiness.conditions:
        print(f"  \033[33m~\033[0m {line[:150]}")
    for line in readiness.blockers:
        print(f"  \033[31m-\033[0m {line[:150]}")

    rule(f"Status: {final.status.value.upper()}")

    for document in final.documents:
        (output / f"{document.doc_type.value}.md").write_text(
            document.content, encoding="utf-8"
        )
        (output / f"{document.doc_type.value}.html").write_text(
            pipeline.generator.to_html(document), encoding="utf-8"
        )
    (output / "audit-trail.txt").write_text(audit.export_text(), encoding="utf-8")

    print(f"  package          {output}")
    print(f"  audit trail      {output / 'audit-trail.txt'} "
          f"({audit.count()} events, chain {'intact' if audit.verify().ok else 'BROKEN'})")
    print(f"  evidence vault   {workspace / 'vault'} "
          f"({len(vault.records())} objects, {vault.verify().objects_checked} verified)")
    print()
    print("  Read the operational qualification report first — it is where the")
    print("  statistics surface:")
    print(f"    {output / 'OQ_REPORT.md'}")
    print("  Then the credibility assessment, structured as the FDA framework's")
    print("  seven steps:")
    print(f"    {output / 'CREDIBILITY_REPORT.md'}")
    print()

    return 0 if final.status.value == "validated" else 1


if __name__ == "__main__":
    sys.exit(main())
