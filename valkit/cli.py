"""The ValKit command line.

Built on argparse rather than a third-party framework, for the same reason the
statistics are pure Python: every dependency in a GxP tool is a supplier to
assess, and a command-line parser is not worth one.

Exit codes are part of the contract, because the primary consumer is a CI job
deciding whether to let a release through:

===  =========================================================================
0    Success, or the acceptance criteria were met.
1    An acceptance criterion was not met, or validated status was not reached.
2    Usage error: a bad argument, an invalid specification, a missing file.
3    Integrity failure: an audit chain, evidence object or signature that does
     not verify. Distinct from 1 because it means recorded evidence cannot be
     trusted, which is a different conversation entirely.
===  =========================================================================

Signature components are never accepted on the command line. A password in
argv lands in the shell history and in the process table, where any other user
on the machine can read it; the prompt and the environment are the only ways
in.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import IntegrityError, SpecError, ValKitError

EXIT_OK = 0
EXIT_ACCEPTANCE_FAILED = 1
EXIT_USAGE = 2
EXIT_INTEGRITY = 3

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


class Output:
    """Human-readable by default, JSON on request."""

    def __init__(self, as_json: bool = False, quiet: bool = False):
        self.as_json = as_json
        self.quiet = quiet

    def say(self, text: str = "") -> None:
        if not self.quiet and not self.as_json:
            print(text)

    def heading(self, text: str) -> None:
        if not self.quiet and not self.as_json:
            print(f"\n{text}\n{'-' * len(text)}")

    def emit(self, payload: Any) -> None:
        if self.as_json:
            from .util import canonical_json

            print(canonical_json(payload))

    def error(self, text: str) -> None:
        print(f"error: {text}", file=sys.stderr)


# --------------------------------------------------------------------------
# Shared construction
# --------------------------------------------------------------------------


def _clock(args: argparse.Namespace):
    from .util import FrozenClock, SystemClock

    if getattr(args, "frozen_clock", None):
        return FrozenClock(args.frozen_clock, step=1.0)
    return SystemClock()


def _workspace(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "workspace", None) or ".valkit")


def _build_pipeline(args: argparse.Namespace, spec):
    """Assemble a pipeline from the flags, defaulting to a local workspace."""
    from .audit.store import AuditTrail
    from .esign.identity import StaticIdentityStore
    from .esign.signatures import SignatureService
    from .evals.providers import judge_for_spec, provider_for_spec
    from .pipeline import ValidationPipeline
    from .vault.store import EvidenceVault

    clock = _clock(args)
    workspace = _workspace(args)
    workspace.mkdir(parents=True, exist_ok=True)

    audit = AuditTrail(workspace / "audit.sqlite", clock)
    vault = EvidenceVault(workspace / "vault", clock)
    identities = StaticIdentityStore(clock)
    signatures = SignatureService(identities, clock, audit)

    return ValidationPipeline(
        provider=provider_for_spec(spec),
        judge=judge_for_spec(spec),
        vault=vault,
        audit=audit,
        signatures=signatures,
        clock=clock,
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, out: Output) -> int:
    """Scaffold a valkit.yaml."""
    target = Path(args.output)
    if target.exists() and not args.force:
        out.error(f"{target} already exists; pass --force to overwrite")
        return EXIT_USAGE

    template = f"""\
apiVersion: valkit/v1
kind: AgentValidation

metadata:
  agent_id: {args.agent_id}
  version: "{args.version}"
  owner: {args.owner or 'you@example.com'}

context_of_use:
  question_of_interest: >
    State the specific question this agent's output helps answer.
  role: >
    State how the output is used, by whom, and whether a human reviews it
    before it has any effect.
  model_influence: medium
  decision_consequence: medium
  human_in_the_loop: true

intended_use:
  in_scope:
    - Describe each task the agent is for
  out_of_scope:
    - Describe each use it must not be put to

gamp:
  category: 5

models:
  primary: {args.model}
  temperature: 0.0
  seed: 0

datasets:
  golden_set:
    ref: datasets/golden.jsonl
    # Pin the digest before a qualification run: an unpinned dataset means the
    # run cannot be proven reproducible. `valkit run` prints the digest.

acceptance:
  # Size the golden set to the target before committing to it:
  #   valkit sample-size --target 0.95
  metrics:
    - name: accuracy
      type: proportion
      scorer: exact_match
      target: 0.95
      confidence: 0.95
      method: clopper_pearson_lower

monitoring:
  schedule: "0 6 * * 1"

signoff:
  approvers: [qa_lead]
  esignature: part11
"""
    target.write_text(template, encoding="utf-8")
    out.say(f"Wrote {target}")
    out.say("Next: edit the context of use and acceptance criteria, then run")
    out.say(f"  valkit validate {target}")
    out.emit({"path": str(target)})
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, out: Output) -> int:
    """Check a specification and show the derived risk assessment."""
    from .spec.derive import derive_all
    from .spec.loader import load_spec_result

    result = load_spec_result(args.spec, strict=not args.lenient)
    bundle = derive_all(result.spec)
    assessment = bundle.assessment

    out.say(f"Specification {args.spec} is valid.")
    out.say(f"  agent          {result.spec.ref}")
    out.say(f"  GAMP category  {int(result.spec.gamp.category.value)}")
    out.say(f"  risk class     {assessment.risk_class.value.upper()}"
            f"{'  (overrides derived ' + assessment.derived_class.value + ')' if assessment.overridden else ''}")
    out.say(f"  requirements   {len(bundle.requirements)}")
    out.say(f"  risks          {len(bundle.risks)}")
    out.say(f"  tests          {len(bundle.tests)}")

    if assessment.escalations:
        out.heading("Risk determination")
        for line in assessment.escalations:
            out.say(f"  * {line}")

    if assessment.required_rigor:
        rigor = assessment.required_rigor
        out.heading("Recommended evidence")
        out.say(f"  minimum qualification set   {rigor.minimum_golden_set} cases")
        out.say(f"  judge calibration required  {'yes' if rigor.judge_calibration_required else 'no'}")
        out.say(f"  adversarial testing         {'yes' if rigor.red_team_required else 'no'}")
        out.say(f"  scheduled re-evaluation     {'yes' if rigor.monitoring_required else 'no'}")

    if result.warnings:
        out.heading(f"Warnings ({len(result.warnings)})")
        for warning in result.warnings:
            out.say(f"  ! {warning}")

    out.emit(
        {
            "valid": True,
            "agent": result.spec.ref,
            "risk_class": assessment.risk_class.value,
            "derived_class": assessment.derived_class.value,
            "requirements": len(bundle.requirements),
            "risks": len(bundle.risks),
            "tests": len(bundle.tests),
            "warnings": result.warnings,
        }
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace, out: Output) -> int:
    """Execute the acceptance battery."""
    from .spec.loader import load_spec

    spec = load_spec(args.spec)
    pipeline = _build_pipeline(args, spec)
    pipeline.ingest_spec(spec)
    pipeline.assess_and_derive()
    dataset = pipeline.load_datasets()
    run = pipeline.run_evals(run_id=args.run_id)

    out.say(f"Run {run.run_id} — {run.status.value}")
    out.say(f"  model    {run.model}")
    out.say(f"  dataset  {run.dataset_ref} ({len(dataset.samples)} cases)")
    out.say(f"  digest   {run.dataset_sha256}")
    out.say(f"  file     {run.environment.get('dataset_file_sha256') or 'not recorded'}")
    out.say("")
    out.say("  Pin either digest in datasets.golden_set.sha256 to make this run repeatable.")

    out.heading("Acceptance")
    for metric in run.metrics:
        flag = "PASS" if metric.passed else "FAIL"
        advisory = " (advisory)" if not metric.critical else ""
        bound = f"{metric.lower_bound:.4f}" if metric.lower_bound is not None else "n/a"
        target = f"{metric.target:.4f}" if metric.target is not None else "n/a"
        out.say(f"  [{flag}] {metric.name}{advisory}")
        out.say(f"         k={metric.k}/{metric.n}  bound={bound}  target={target}")
        if metric.failing_sample_ids:
            shown = ", ".join(metric.failing_sample_ids[:6])
            more = f" (+{len(metric.failing_sample_ids) - 6})" if len(metric.failing_sample_ids) > 6 else ""
            out.say(f"         failing: {shown}{more}")

    if run.calibration is not None:
        flag = "PASS" if run.calibration.passed else "FAIL"
        out.say("")
        out.say(
            f"  [{flag}] judge calibration  kappa={run.calibration.cohen_kappa:.3f} "
            f"n={run.calibration.n} required>={run.calibration.min_required:.2f}"
        )

    out.emit(
        {
            "run_id": run.run_id,
            "status": run.status.value,
            "passed": run.passed,
            "dataset_sha256": run.dataset_sha256,
            "dataset_file_sha256": run.environment.get("dataset_file_sha256"),
            "metrics": [m.to_dict() for m in run.metrics],
            "calibration": run.calibration.to_dict() if run.calibration else None,
        }
    )
    return EXIT_OK if run.passed else EXIT_ACCEPTANCE_FAILED


def cmd_package(args: argparse.Namespace, out: Output) -> int:
    """Run the whole pipeline and write the package to a directory."""
    from .spec.loader import load_spec

    spec = load_spec(args.spec)
    pipeline = _build_pipeline(args, spec)
    result = pipeline.run_all(spec, run_id=args.run_id)

    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for document in result.record.documents:
        path = destination / f"{document.doc_type.value}.md"
        path.write_text(document.content, encoding="utf-8")
        written.append(str(path))
        if args.html:
            html_path = destination / f"{document.doc_type.value}.html"
            html_path.write_text(pipeline.generator.to_html(document), encoding="utf-8")
            written.append(str(html_path))

    out.say(f"Validation package for {result.record.agent_id} v{result.record.agent_version}")
    out.say(f"  status     {result.record.status.value}")
    out.say(f"  documents  {len(result.record.documents)} written to {destination}")
    if result.run:
        out.say(f"  run        {result.run.run_id}")

    # `Readiness.__bool__` reports whether the record is ready, so testing the
    # object for truthiness would suppress the explanation exactly when the
    # record is not ready and the explanation is what the caller needs.
    readiness = result.readiness
    if readiness is not None and readiness.satisfied:
        out.heading("Satisfied")
        for line in readiness.satisfied:
            out.say(f"  + {line}")
    if readiness is not None and readiness.conditions:
        out.heading("Outstanding conditions")
        for line in readiness.conditions:
            out.say(f"  ~ {line}")
    if readiness is not None and readiness.blockers:
        out.heading("Not yet validated because")
        for line in readiness.blockers:
            out.say(f"  - {line}")

    out.emit(
        {
            "status": result.record.status.value,
            "documents": written,
            "run_id": result.run.run_id if result.run else None,
            "ready": bool(readiness is not None and readiness.ready),
            "blockers": readiness.blockers if readiness is not None else [],
            "conditions": readiness.conditions if readiness is not None else [],
        }
    )
    return EXIT_OK if result.validated else EXIT_ACCEPTANCE_FAILED


def cmd_rtm(args: argparse.Namespace, out: Output) -> int:
    """Print the traceability matrix."""
    from .spec.derive import derive_all
    from .spec.loader import load_spec
    from .trace.graph import TraceabilityGraph
    from .trace.rtm import build_rtm, render_csv, render_markdown

    bundle = derive_all(load_spec(args.spec))
    graph = TraceabilityGraph.from_records(
        requirements=bundle.requirements, risks=bundle.risks, tests=bundle.tests
    )
    rows = build_rtm(graph)
    if args.csv:
        print(render_csv(rows), end="")
    elif out.as_json:
        out.emit({"rows": [r.__dict__ for r in rows], "coverage": graph.coverage().__dict__})
    else:
        print(render_markdown(rows, graph.coverage()))
    return EXIT_OK


def cmd_sign(args: argparse.Namespace, out: Output) -> int:
    """Apply a Part 11 electronic signature to a document."""
    from .audit.store import AuditTrail
    from .esign.identity import (
        COMPONENT_PASSWORD,
        COMPONENT_USER_ID,
        StaticIdentityStore,
    )
    from .esign.signatures import SignatureService
    from .models import DocumentType
    from .util import sha256_text

    path = Path(args.document)
    if not path.is_file():
        out.error(f"no such document: {path}")
        return EXIT_USAGE

    # Never from argv: a credential on a command line is in the shell history
    # and visible in the process table.
    password = os.environ.get("VALKIT_SIGNING_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            out.error(
                "no signing credential available. Set VALKIT_SIGNING_PASSWORD, or run "
                "interactively so the password can be prompted for. A password must "
                "never be passed as a command-line argument."
            )
            return EXIT_USAGE
        password = getpass.getpass(f"Password for {args.user}: ")

    clock = _clock(args)
    workspace = _workspace(args)
    audit = AuditTrail(workspace / "audit.sqlite", clock)
    identities = StaticIdentityStore(clock)
    identities.add(args.user, args.printed_name, password)
    service = SignatureService(identities, clock, audit)

    content = path.read_text(encoding="utf-8")
    from .models import Document, DocumentStatus

    document = Document(
        doc_id=args.doc_id or path.stem,
        doc_type=DocumentType.VSR,
        title=path.stem,
        agent_id=args.agent_id or "",
        agent_version="",
        content=content,
        content_sha256=sha256_text(content),
        generated_at=clock.now_iso(),
        status=DocumentStatus.DRAFT,
    )
    signature = service.sign(
        document,
        args.user,
        args.meaning,
        {COMPONENT_USER_ID: args.user, COMPONENT_PASSWORD: password},
        reason=args.reason,
    )

    manifest = service.manifest(signature)
    if args.in_place:
        path.write_text(content.rstrip("\n") + "\n\n" + manifest + "\n", encoding="utf-8")
        out.say(f"Signed {path} and appended the signature manifest.")
    else:
        out.say(manifest)
    out.emit({"signature_id": signature.signature_id, "document_sha256": signature.document_sha256})
    return EXIT_OK


def cmd_audit(args: argparse.Namespace, out: Output) -> int:
    """Show or verify the audit chain."""
    from .audit.store import AuditTrail

    path = Path(args.path or (_workspace(args) / "audit.sqlite"))
    if not path.exists():
        out.error(f"no audit trail at {path}")
        return EXIT_USAGE

    trail = AuditTrail(path, _clock(args))
    verification = trail.verify()

    if args.verify:
        out.say(f"Audit chain: {'INTACT' if verification.ok else 'BROKEN'}")
        out.say(f"  records checked  {verification.records_checked}")
        out.say(f"  chain digest     {verification.chain_digest}")
        if not verification.ok:
            out.say(f"  first bad record {verification.first_bad_seq}")
            out.say(f"  reason           {verification.reason}")
        out.emit(
            {
                "ok": verification.ok,
                "records_checked": verification.records_checked,
                "chain_digest": verification.chain_digest,
                "first_bad_seq": verification.first_bad_seq,
                "reason": verification.reason,
            }
        )
        return EXIT_OK if verification.ok else EXIT_INTEGRITY

    if out.as_json:
        out.emit({"records": [r.to_dict() for r in trail.filter(limit=args.limit)]})
    else:
        print(trail.export_text(limit=args.limit))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace, out: Output) -> int:
    """Verify the audit chain and the evidence vault together."""
    from .audit.store import AuditTrail
    from .vault.store import EvidenceVault

    workspace = _workspace(args)
    problems: list[str] = []
    payload: dict[str, Any] = {}

    audit_path = workspace / "audit.sqlite"
    if audit_path.exists():
        chain = AuditTrail(audit_path, _clock(args)).verify()
        payload["audit"] = {"ok": chain.ok, "records": chain.records_checked, "reason": chain.reason}
        out.say(f"Audit chain     {'INTACT' if chain.ok else 'BROKEN'}  "
                f"({chain.records_checked} records)")
        if not chain.ok:
            problems.append(f"audit: {chain.reason}")
    else:
        out.say("Audit chain     not present")

    vault_path = workspace / "vault"
    if vault_path.exists():
        result = EvidenceVault(vault_path, _clock(args)).verify()
        payload["vault"] = {
            "ok": result.ok,
            "objects": result.objects_checked,
            "reason": result.reason,
        }
        out.say(f"Evidence vault  {'INTACT' if result.ok else 'BROKEN'}  "
                f"({result.objects_checked} objects)")
        if not result.ok:
            problems.append(f"vault: {result.reason}")
    else:
        out.say("Evidence vault  not present")

    if problems:
        out.say("")
        for problem in problems:
            out.say(f"  ! {problem}")
    out.emit({**payload, "ok": not problems})
    return EXIT_INTEGRITY if problems else EXIT_OK


def cmd_sample_size(args: argparse.Namespace, out: Output) -> int:
    """Size a qualification set for a target and confidence."""
    from .stats.proportions import (
        clopper_pearson_lower,
        max_failures_for_n,
        min_n_with_failures,
    )

    target, confidence = args.target, args.confidence
    out.say(f"To demonstrate a pass rate of at least {target} with {confidence:.0%} confidence:")
    out.say("")
    out.say("  failures allowed   minimum cases")
    rows = []
    for failures in range(0, args.failures + 1):
        n = min_n_with_failures(target, confidence, failures)
        out.say(f"  {failures:>16}   {n}")
        rows.append({"failures": failures, "minimum_n": n})

    if args.n:
        tolerable = max_failures_for_n(args.n, target, confidence)
        out.say("")
        if tolerable < 0:
            out.say(
                f"  With {args.n} cases the target {target} cannot be demonstrated at "
                f"{confidence:.0%} confidence even if every case passes. The criterion is "
                f"unachievable as written; enlarge the set or lower the target."
            )
        else:
            bound = clopper_pearson_lower(args.n - tolerable, args.n, confidence)
            out.say(
                f"  With {args.n} cases you may tolerate at most {tolerable} failure(s) "
                f"(bound {bound:.4f})."
            )
        rows.append({"n": args.n, "max_failures": tolerable})

    out.say("")
    out.say("  These figures assume the cases are independent and representative of the")
    out.say("  intended use. That assumption rests on curating the set, not on the arithmetic.")
    out.emit({"target": target, "confidence": confidence, "rows": rows})
    return EXIT_OK


def cmd_docs(args: argparse.Namespace, out: Output) -> int:
    """Generate the document package without signing."""
    args.output = args.output or "out"
    return cmd_package(args, out)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valkit",
        description="Part 11 validation-as-code for LLM agents in GxP workflows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 success, 1 acceptance not met, 2 usage error, "
            "3 integrity failure."
        ),
    )
    parser.add_argument("--version", action="version", version=f"valkit {__version__}")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--quiet", "-q", action="store_true", help="suppress human-readable output")
    parser.add_argument(
        "--workspace", default=".valkit", help="directory for the audit trail and evidence vault"
    )
    parser.add_argument(
        "--frozen-clock",
        metavar="ISO8601",
        help="use a fixed clock, for reproducible output in tests and demonstrations",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="scaffold a valkit.yaml")
    p.add_argument("--agent-id", default="my-agent")
    p.add_argument("--version", dest="version", default="0.1.0")
    p.add_argument("--owner", default="")
    p.add_argument("--model", default="fixture/my-agent")
    p.add_argument("-o", "--output", default="valkit.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="check a specification and show the risk assessment")
    p.add_argument("spec")
    p.add_argument("--lenient", action="store_true", help="warn about unknown keys instead of failing")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="execute the acceptance battery")
    p.add_argument("spec")
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("docs", help="generate the document package")
    p.add_argument("spec")
    p.add_argument("-o", "--output", default="out")
    p.add_argument("--run-id")
    p.add_argument("--html", action="store_true", help="also write printable HTML")
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("package", help="run the whole pipeline and write the package")
    p.add_argument("spec")
    p.add_argument("-o", "--output", default="out")
    p.add_argument("--run-id")
    p.add_argument("--html", action="store_true", help="also write printable HTML")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("rtm", help="print the traceability matrix")
    p.add_argument("spec")
    p.add_argument("--csv", action="store_true")
    p.set_defaults(func=cmd_rtm)

    p = sub.add_parser("sign", help="apply a Part 11 electronic signature")
    p.add_argument("document")
    p.add_argument("--user", required=True)
    p.add_argument("--printed-name", required=True, help="the signer's full name, per 11.50(a)(1)")
    p.add_argument(
        "--meaning",
        default="approved",
        choices=["authored", "reviewed", "approved", "executed", "verified", "rejected"],
    )
    p.add_argument("--reason", default="")
    p.add_argument("--doc-id")
    p.add_argument("--agent-id")
    p.add_argument("--in-place", action="store_true", help="append the manifest to the document")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("audit", help="show or verify the audit trail")
    p.add_argument("--path")
    p.add_argument("--limit", type=int)
    p.add_argument("--verify", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("verify", help="verify the audit chain and the evidence vault")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("sample-size", help="size a qualification set")
    p.add_argument("--target", type=float, default=0.95)
    p.add_argument("--confidence", type=float, default=0.95)
    p.add_argument("--failures", type=int, default=3, help="show sizes up to this many failures")
    p.add_argument("--n", type=int, help="also report the failures tolerable at this size")
    p.set_defaults(func=cmd_sample_size)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Output(as_json=args.json, quiet=args.quiet)

    try:
        return int(args.func(args, out))
    except SpecError as error:
        out.error(str(error))
        return EXIT_USAGE
    except IntegrityError as error:
        out.error(str(error))
        return EXIT_INTEGRITY
    except ValKitError as error:
        out.error(str(error))
        return EXIT_USAGE
    except FileNotFoundError as error:
        out.error(str(error))
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        out.error("interrupted")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
