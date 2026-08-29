"""The scheduled re-evaluation worker.

A validated agent does not stay validated by itself. The model behind it is
replaced, the prompt is tuned, the data it sees shifts, and the claim in a
signed package is a claim about a system that no longer exists. GAMP 5's
periodic review and Appendix D11's monitoring expectation both land here, and
so does the practical point: an agent nobody re-evaluates is an agent whose
validation is a document rather than a fact.

Two ways to run it, both named by the deployment in ``infra/terraform``:

``python -m valkit.worker``
    A long-running service. Wakes on a tick, re-evaluates whatever the cron
    expressions in the specifications say is due, and sleeps again.

``python -m valkit.worker --scheduled``
    One pass and exit, for an EventBridge-driven Fargate task. Same work, no
    loop. The exit code is the contract: ``0`` nothing to report, ``1`` a drift
    alert was raised, ``2`` a usage error, ``3`` an integrity failure.

**A re-evaluation never signs anything, and never restores validated status.**
It produces evidence and, where a control rule trips, opens a change control. A
worker that could sign would be a worker that could grant validated status to an
agent no human had looked at, which is the opposite of what the signature is
for.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .audit.store import AuditTrail
from .change.control import ChangeControlRegister
from .drift.monitor import DriftMonitor, JsonMonitoringStore, next_due
from .errors import IntegrityError, ValKitError
from .models import AgentSpec, MonitoringSpec
from .spec.loader import load_spec
from .util import Clock, SystemClock, format_utc
from .vault.store import EvidenceVault

__all__ = ["Worker", "WorkerResult", "AgentWork", "build_worker", "main"]

LOGGER = logging.getLogger("valkit.worker")

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_USAGE = 2
EXIT_INTEGRITY = 3

DEFAULT_TICK_SECONDS = 60


@dataclass
class AgentWork:
    """What one pass did for one agent."""

    agent_id: str
    spec_path: str
    due: bool
    run_id: str | None = None
    passed: bool | None = None
    alerts: list[str] = field(default_factory=list)
    change_controls: list[str] = field(default_factory=list)
    error: str | None = None
    next_due_at: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"{self.agent_id}: failed — {self.error}"
        if not self.due:
            return f"{self.agent_id}: not due (next {self.next_due_at or 'unscheduled'})"
        verdict = "met its criteria" if self.passed else "DID NOT meet its criteria"
        line = f"{self.agent_id}: re-evaluated ({self.run_id}), {verdict}"
        if self.alerts:
            line += f"; {len(self.alerts)} drift alert(s)"
        if self.change_controls:
            line += f"; opened {', '.join(self.change_controls)}"
        return line


@dataclass
class WorkerResult:
    """What one pass did, and what the exit code should be."""

    started_at: str
    work: list[AgentWork] = field(default_factory=list)
    integrity_ok: bool = True
    integrity_reason: str = ""

    @property
    def alerts(self) -> list[str]:
        return [alert for item in self.work for alert in item.alerts]

    @property
    def evaluated(self) -> list[AgentWork]:
        return [item for item in self.work if item.due and item.error is None]

    @property
    def exit_code(self) -> int:
        if not self.integrity_ok:
            return EXIT_INTEGRITY
        if self.alerts or any(item.passed is False for item in self.evaluated):
            return EXIT_ALERT
        return EXIT_OK

    def report(self) -> str:
        lines = [f"ValKit worker pass at {self.started_at}"]
        if not self.integrity_ok:
            lines.append(f"  INTEGRITY FAILURE: {self.integrity_reason}")
        if not self.work:
            lines.append("  no specifications found")
        lines.extend(f"  {item.summary()}" for item in self.work)
        return "\n".join(lines)


class Worker:
    """Finds the agents whose re-evaluation is due, and re-evaluates them."""

    def __init__(
        self,
        spec_paths: Sequence[str | os.PathLike[str]],
        *,
        clock: Clock | None = None,
        audit: AuditTrail | None = None,
        vault: Any = None,
        monitor: DriftMonitor | None = None,
        change_register: ChangeControlRegister | None = None,
        base_dir: str | os.PathLike[str] | None = None,
        force: bool = False,
    ):
        self._clock = clock or SystemClock()
        self._spec_paths = [Path(p) for p in spec_paths]
        self.audit = audit
        self.vault = vault
        self.monitor = monitor or DriftMonitor(clock=self._clock, audit=audit)
        self.change_register = change_register
        self._base_dir = base_dir
        # Re-evaluate regardless of schedule. For an operator who has just
        # changed something and wants the evidence now.
        self.force = force
        self._stopping = False

    # -- one pass ----------------------------------------------------------

    def run_once(self) -> WorkerResult:
        """Check integrity, then re-evaluate whatever is due."""
        result = WorkerResult(started_at=self._clock.now_iso())

        # Integrity first. Producing new evidence on top of a broken chain would
        # extend a record nobody can rely on, so a failure here stops the pass
        # rather than being reported alongside the results.
        ok, reason = self._check_integrity()
        result.integrity_ok = ok
        result.integrity_reason = reason
        if not ok:
            LOGGER.error("integrity check failed: %s", reason)
            return result

        for path in self._spec_paths:
            result.work.append(self._process(path))
        return result

    def _check_integrity(self) -> tuple[bool, str]:
        if self.audit is not None:
            chain = self.audit.verify()
            if not chain.ok:
                return False, f"audit chain: {chain.reason}"
        if self.vault is not None:
            verification = self.vault.verify()
            if not verification.ok:
                return False, f"evidence vault: {verification.reason}"
        return True, ""

    def _process(self, path: Path) -> AgentWork:
        try:
            spec = load_spec(path)
        except ValKitError as error:
            return AgentWork(
                agent_id=path.stem, spec_path=str(path), due=False, error=str(error)
            )

        monitoring = spec.monitoring
        due = self.force or self.monitor.due(spec.agent_id, monitoring, now=self._clock.now())
        work = AgentWork(
            agent_id=spec.agent_id,
            spec_path=str(path),
            due=due,
            next_due_at=self._next_due(monitoring),
        )
        if not due:
            return work

        try:
            self._reevaluate(spec, monitoring, work)
        except IntegrityError:
            raise
        except ValKitError as error:
            work.error = str(error)
            LOGGER.warning("re-evaluation of %s failed: %s", spec.agent_id, error)
        return work

    def _reevaluate(self, spec: AgentSpec, monitoring: MonitoringSpec, work: AgentWork) -> None:
        from .evals.providers import judge_for_spec, provider_for_spec
        from .pipeline import ValidationPipeline

        pipeline = ValidationPipeline(
            provider=provider_for_spec(spec, base_dir=self._base_dir),
            judge=judge_for_spec(spec),
            vault=self.vault,
            audit=self.audit,
            change_register=self.change_register,
            clock=self._clock,
            base_dir=self._base_dir,
        )
        pipeline.ingest_spec(spec)
        pipeline.assess_and_derive()
        pipeline.load_datasets()
        run = pipeline.run_evals()

        work.run_id = run.run_id
        work.passed = run.passed
        self.monitor.record(run)

        targets = {metric.name: metric.target for metric in run.metrics}
        for alert in self.monitor.check_all(spec.agent_id, monitoring, targets):
            work.alerts.append(self.monitor.render_alert(alert))
            if alert.change_control_id:
                work.change_controls.append(alert.change_control_id)

        if self.audit is not None:
            self.audit.append(
                actor="valkit-worker",
                action="monitoring.reevaluated",
                entity_type="agent",
                entity_id=spec.ref,
                payload={
                    "run_id": run.run_id,
                    "passed": run.passed,
                    "alerts": len(work.alerts),
                },
                reason="Scheduled re-evaluation",
            )

    def _next_due(self, monitoring: MonitoringSpec) -> str | None:
        if not monitoring.schedule:
            return None
        moment = next_due(monitoring.schedule, self._clock.now())
        return format_utc(moment) if moment else None

    # -- the loop ----------------------------------------------------------

    def stop(self) -> None:
        self._stopping = True

    def serve(self, *, tick_seconds: int = DEFAULT_TICK_SECONDS, sleep: Any = time.sleep) -> int:
        """Run passes until stopped. Returns the last pass's exit code.

        A pass that fails does not take the process down: the next tick tries
        again, and the failure is in the trail either way. An integrity failure
        is the exception — the process exits so that the deployment's alarms
        see a task that stopped, rather than one quietly looping on evidence it
        cannot verify.
        """
        code = EXIT_OK
        while not self._stopping:
            result = self.run_once()
            code = result.exit_code
            LOGGER.info("%s", result.report())
            if not result.integrity_ok:
                return EXIT_INTEGRITY
            if self._stopping:
                break
            sleep(tick_seconds)
        return code


def build_worker(
    spec_paths: Sequence[str | os.PathLike[str]],
    workspace: str | os.PathLike[str] = ".valkit",
    *,
    clock: Clock | None = None,
    force: bool = False,
    base_dir: str | os.PathLike[str] | None = None,
) -> Worker:
    """Assemble a worker against a workspace on disk."""
    clock = clock or SystemClock()
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)

    audit = AuditTrail(root / "audit.sqlite", clock)
    vault = _vault(root, clock)
    change_register = ChangeControlRegister(
        root / "change-control.json", clock=clock, audit=audit
    )
    monitor = DriftMonitor(
        JsonMonitoringStore(root / "monitoring.jsonl"),
        clock=clock,
        audit=audit,
        change_register=change_register,
    )
    return Worker(
        spec_paths,
        clock=clock,
        audit=audit,
        vault=vault,
        monitor=monitor,
        change_register=change_register,
        base_dir=base_dir,
        force=force,
    )


def _vault(root: Path, clock: Clock) -> Any:
    bucket = os.environ.get("VALKIT_EVIDENCE_BUCKET")
    if not bucket:
        return EvidenceVault(root / "vault", clock)

    from .vault.s3 import S3EvidenceVault

    return S3EvidenceVault(
        bucket=bucket,
        clock=clock,
        kms_key_id=os.environ.get("VALKIT_EVIDENCE_KMS_KEY") or None,
        object_lock_mode=os.environ.get("VALKIT_OBJECT_LOCK_MODE", "COMPLIANCE"),
        retention_years=int(os.environ.get("VALKIT_RETENTION_YEARS", "7")),
    )


def _discover(paths: Sequence[str]) -> list[Path]:
    """Expand directories into the specifications they contain."""
    found: list[Path] = []
    for entry in paths:
        path = Path(entry)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.yaml") if p.is_file()))
        elif path.is_file():
            found.append(path)
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m valkit.worker",
        description="Re-evaluate validated agents on their monitoring schedule.",
    )
    parser.add_argument(
        "specs",
        nargs="*",
        default=None,
        help=(
            "Specification files or directories to watch. Defaults to "
            "$VALKIT_SPEC_DIR, or ./specs."
        ),
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Run one pass and exit, for an externally scheduled invocation.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate every agent regardless of its schedule.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("VALKIT_WORKSPACE", ".valkit"),
        help="Where the audit trail, vault and monitoring history live.",
    )
    parser.add_argument(
        "--tick-seconds",
        type=int,
        default=int(os.environ.get("VALKIT_TICK_SECONDS", DEFAULT_TICK_SECONDS)),
        help="Seconds between passes when running as a service.",
    )
    parser.add_argument("--base-dir", default=None, help="Root for relative dataset paths.")
    parser.add_argument("--quiet", action="store_true", help="Report only failures.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    roots = args.specs or [os.environ.get("VALKIT_SPEC_DIR", "specs")]
    spec_paths = _discover(roots)
    if not spec_paths:
        print(
            f"no specifications found in {', '.join(roots)}. Pass one or more paths, or set "
            "VALKIT_SPEC_DIR.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    worker = build_worker(
        spec_paths, args.workspace, force=args.force, base_dir=args.base_dir
    )

    if args.scheduled:
        result = worker.run_once()
        print(result.report())
        for alert in result.alerts:
            print()
            print(alert)
        return result.exit_code

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop())
    return worker.serve(tick_seconds=args.tick_seconds)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
