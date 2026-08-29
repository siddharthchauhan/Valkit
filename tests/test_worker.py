"""Tests for the scheduled re-evaluation worker.

Three behaviours are the point of the module and are asserted directly: it
re-evaluates only what the schedule says is due, it never signs anything, and it
refuses to produce new evidence on top of an audit chain that does not verify.
"""

from __future__ import annotations

import pathlib

import pytest

from valkit.util import FrozenClock
from valkit.worker import (
    EXIT_ALERT,
    EXIT_INTEGRITY,
    EXIT_OK,
    EXIT_USAGE,
    AgentWork,
    Worker,
    WorkerResult,
    build_worker,
    main,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "valkit.yaml"


@pytest.fixture
def worker(tmp_path):
    return build_worker(
        [SPEC],
        tmp_path / "workspace",
        clock=FrozenClock("2026-01-01T00:00:00Z", step=1.0),
        base_dir=ROOT,
    )


class TestOnePass:
    def test_first_pass_evaluates_because_there_is_no_history(self, worker):
        """An agent with no observations has never been re-evaluated."""
        result = worker.run_once()
        assert len(result.work) == 1
        work = result.work[0]
        assert work.due is True
        assert work.run_id
        assert work.passed is True
        assert result.exit_code == EXIT_OK

    def test_the_second_pass_is_not_due(self, worker):
        worker.run_once()
        result = worker.run_once()
        assert result.work[0].due is False
        assert result.work[0].run_id is None
        assert result.work[0].next_due_at

    def test_force_ignores_the_schedule(self, tmp_path):
        worker = build_worker(
            [SPEC],
            tmp_path / "workspace",
            clock=FrozenClock("2026-01-01T00:00:00Z", step=1.0),
            base_dir=ROOT,
            force=True,
        )
        worker.run_once()
        assert worker.run_once().work[0].due is True

    def test_the_run_lands_in_the_monitoring_series(self, worker):
        worker.run_once()
        metrics = worker.monitor.store.metrics("rave-als-generator")
        assert "field_accuracy" in metrics
        assert worker.monitor.store.series("rave-als-generator", "field_accuracy")

    def test_the_re_evaluation_is_recorded_against_the_worker(self, worker):
        worker.run_once()
        actions = [r.action for r in worker.audit.records()]
        assert "monitoring.reevaluated" in actions
        record = next(r for r in worker.audit.records() if r.action == "monitoring.reevaluated")
        assert record.actor == "valkit-worker"

    def test_it_never_signs_anything(self, worker):
        """A worker that could sign could grant validated status to an agent no
        human had looked at."""
        worker.run_once()
        actions = {r.action for r in worker.audit.records()}
        # Guard against passing vacuously: the pass must have written a trail.
        assert "monitoring.reevaluated" in actions
        assert not any(action.startswith("signature.") for action in actions)
        assert "document.signed" not in actions

    def test_an_unreadable_specification_is_reported_not_raised(self, tmp_path):
        bad = tmp_path / "broken.yaml"
        bad.write_text("apiVersion: valkit/v1\nkind: AgentValidation\n")
        worker = build_worker(
            [bad], tmp_path / "workspace", clock=FrozenClock(step=1.0), base_dir=ROOT
        )
        result = worker.run_once()
        assert result.work[0].error
        assert result.exit_code == EXIT_OK

    def test_one_bad_specification_does_not_stop_the_others(self, tmp_path):
        bad = tmp_path / "broken.yaml"
        bad.write_text("nonsense: true\n")
        worker = build_worker(
            [bad, SPEC], tmp_path / "workspace", clock=FrozenClock(step=1.0), base_dir=ROOT
        )
        result = worker.run_once()
        assert result.work[0].error
        assert result.work[1].run_id


class TestIntegrityFirst:
    def test_a_broken_chain_stops_the_pass(self, worker):
        """Producing new evidence on top of a broken chain would extend a
        record nobody can rely on."""
        worker.audit.append(
            actor="someone", action="test.event", entity_type="thing", entity_id="x"
        )
        with worker.audit._lock:
            worker.audit._connection.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            worker.audit._connection.execute(
                "UPDATE audit_log SET actor = 'else' WHERE seq = 2"
            )

        result = worker.run_once()
        assert result.integrity_ok is False
        assert result.work == []
        assert result.exit_code == EXIT_INTEGRITY

    def test_a_corrupted_evidence_object_stops_the_pass(self, worker):
        worker.run_once()
        record = worker.vault.records()[0]
        path = worker.vault._object_path(record.evidence_id)
        path.chmod(0o644)
        path.write_bytes(b"tampered")

        result = worker.run_once()
        assert result.integrity_ok is False
        assert result.exit_code == EXIT_INTEGRITY

    def test_the_serve_loop_exits_on_an_integrity_failure(self, worker):
        """A task that stops is visible to the alarms; one that loops quietly
        on evidence it cannot verify is not."""
        worker.audit.append(
            actor="someone", action="test.event", entity_type="thing", entity_id="x"
        )
        with worker.audit._lock:
            worker.audit._connection.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            worker.audit._connection.execute(
                "UPDATE audit_log SET actor = 'else' WHERE seq = 2"
            )

        assert worker.serve(sleep=lambda _: None) == EXIT_INTEGRITY


class TestExitCodes:
    def test_nothing_to_report_is_zero(self):
        result = WorkerResult(started_at="2026-01-01T00:00:00Z")
        assert result.exit_code == EXIT_OK

    def test_a_failed_criterion_is_one(self):
        result = WorkerResult(
            started_at="2026-01-01T00:00:00Z",
            work=[AgentWork(agent_id="a", spec_path="a.yaml", due=True, passed=False)],
        )
        assert result.exit_code == EXIT_ALERT

    def test_a_drift_alert_is_one(self):
        result = WorkerResult(
            started_at="2026-01-01T00:00:00Z",
            work=[
                AgentWork(
                    agent_id="a", spec_path="a.yaml", due=True, passed=True, alerts=["drift"]
                )
            ],
        )
        assert result.exit_code == EXIT_ALERT

    def test_integrity_outranks_everything(self):
        result = WorkerResult(
            started_at="2026-01-01T00:00:00Z",
            work=[AgentWork(agent_id="a", spec_path="a.yaml", due=True, passed=True)],
            integrity_ok=False,
            integrity_reason="chain broken",
        )
        assert result.exit_code == EXIT_INTEGRITY

    def test_a_failure_that_is_not_due_does_not_count(self):
        """An agent that is not due was not evaluated; it has no verdict."""
        result = WorkerResult(
            started_at="2026-01-01T00:00:00Z",
            work=[AgentWork(agent_id="a", spec_path="a.yaml", due=False)],
        )
        assert result.exit_code == EXIT_OK


class TestServeLoop:
    def test_stops_when_asked(self, worker):
        passes = []

        def sleep(_seconds):
            passes.append(1)
            if len(passes) >= 2:
                worker.stop()

        assert worker.serve(tick_seconds=0, sleep=sleep) in (EXIT_OK, EXIT_ALERT)
        assert len(passes) == 2


class TestCommandLine:
    def test_one_pass_and_exit(self, tmp_path, capsys):
        code = main(
            [
                "--scheduled",
                "--workspace",
                str(tmp_path / "workspace"),
                "--base-dir",
                str(ROOT),
                str(SPEC),
            ]
        )
        assert code == EXIT_OK
        assert "re-evaluated" in capsys.readouterr().out

    def test_no_specifications_is_a_usage_error(self, tmp_path, capsys):
        code = main(
            ["--scheduled", "--workspace", str(tmp_path / "workspace"), str(tmp_path / "none")]
        )
        assert code == EXIT_USAGE
        assert "no specifications found" in capsys.readouterr().err

    def test_a_directory_is_expanded(self, tmp_path, capsys):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "agent.yaml").write_text(SPEC.read_text())
        code = main(
            [
                "--scheduled",
                "--workspace",
                str(tmp_path / "workspace"),
                "--base-dir",
                str(ROOT),
                str(specs),
            ]
        )
        assert code == EXIT_OK
        assert "rave-als-generator" in capsys.readouterr().out


class TestReporting:
    def test_the_report_names_what_happened(self, worker):
        report = worker.run_once().report()
        assert "rave-als-generator" in report
        assert "re-evaluated" in report

    def test_a_not_due_agent_says_when_it_next_is(self, worker):
        worker.run_once()
        report = worker.run_once().report()
        assert "not due" in report
        assert "2026-" in report
