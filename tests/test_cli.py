"""Tests for the command line.

The exit codes are a contract with CI, so each is tested. So is the rule that a
signing credential is never accepted on the command line.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from valkit.cli import (
    EXIT_ACCEPTANCE_FAILED,
    EXIT_INTEGRITY,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = str(ROOT / "examples" / "valkit.yaml")
CLOCK = ["--frozen-clock", "2026-01-01T09:00:00Z"]


def run(args, capsys):
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestParser:
    def test_every_subcommand_is_registered(self):
        parser = build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        names = set(actions[-1].choices)
        assert {
            "init", "validate", "run", "docs", "package", "rtm", "sign", "audit",
            "verify", "sample-size",
        } <= names

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestValidate:
    def test_a_valid_spec_exits_zero(self, capsys):
        code, out, _ = run(["validate", SPEC], capsys)
        assert code == EXIT_OK
        assert "is valid" in out
        assert "risk class     MEDIUM" in out

    def test_an_invalid_spec_is_a_usage_error(self, capsys, workdir):
        bad = workdir / "bad.yaml"
        bad.write_text("apiVersion: valkit/v1\nkind: AgentValidation\n", encoding="utf-8")
        code, _, err = run(["validate", str(bad)], capsys)
        assert code == EXIT_USAGE
        assert "metadata" in err

    def test_a_missing_file_is_a_usage_error(self, capsys, workdir):
        code, _, err = run(["validate", str(workdir / "absent.yaml")], capsys)
        assert code == EXIT_USAGE
        assert "file not found" in err

    def test_json_output_is_machine_readable(self, capsys):
        code, out, _ = run(["--json", "validate", SPEC], capsys)
        assert code == EXIT_OK
        payload = json.loads(out)
        assert payload["valid"] is True
        assert payload["agent"] == "rave-als-generator@2.3.1"
        assert payload["risk_class"] == "medium"

    def test_quiet_suppresses_human_output(self, capsys):
        code, out, _ = run(["--quiet", "validate", SPEC], capsys)
        assert code == EXIT_OK
        assert out == ""


class TestSampleSize:
    def test_prints_the_sizing_table(self, capsys):
        code, out, _ = run(["sample-size", "--target", "0.95"], capsys)
        assert code == EXIT_OK
        assert "59" in out and "93" in out

    def test_reports_tolerable_failures_for_a_given_size(self, capsys):
        code, out, _ = run(["sample-size", "--target", "0.95", "--n", "64"], capsys)
        assert code == EXIT_OK
        assert "at most 0 failure" in out

    def test_states_when_a_target_is_unachievable(self, capsys):
        code, out, _ = run(["sample-size", "--target", "0.99", "--n", "10"], capsys)
        assert code == EXIT_OK
        assert "cannot be demonstrated" in out

    def test_states_the_independence_assumption(self, capsys):
        _, out, _ = run(["sample-size"], capsys)
        assert "independent and representative" in out


class TestInit:
    def test_scaffolds_a_loadable_spec(self, capsys, workdir):
        target = workdir / "valkit.yaml"
        code, out, _ = run(["init", "-o", str(target), "--agent-id", "demo-agent"], capsys)
        assert code == EXIT_OK
        assert target.exists()

        from valkit.spec import load_spec

        spec = load_spec(target)
        assert spec.agent_id == "demo-agent"

    def test_refuses_to_overwrite_without_force(self, capsys, workdir):
        target = workdir / "valkit.yaml"
        target.write_text("existing", encoding="utf-8")
        code, _, err = run(["init", "-o", str(target)], capsys)
        assert code == EXIT_USAGE
        assert "already exists" in err

    def test_force_overwrites(self, capsys, workdir):
        target = workdir / "valkit.yaml"
        target.write_text("existing", encoding="utf-8")
        code, _, _ = run(["init", "-o", str(target), "--force"], capsys)
        assert code == EXIT_OK
        assert "apiVersion" in target.read_text()


class TestRun:
    def test_a_passing_run_exits_zero(self, capsys, workdir, monkeypatch):
        monkeypatch.chdir(ROOT)
        code, out, _ = run([*CLOCK, "--workspace", str(workdir / "ws"), "run", SPEC], capsys)
        assert code == EXIT_OK
        assert "[PASS] field_accuracy" in out
        assert "k=61/64" in out

    def test_prints_both_digests_so_one_can_be_pinned(self, capsys, workdir, monkeypatch):
        monkeypatch.chdir(ROOT)
        _, out, _ = run([*CLOCK, "--workspace", str(workdir / "ws"), "run", SPEC], capsys)
        assert "Pin either digest" in out

    def test_a_failing_run_exits_one(self, capsys, workdir, monkeypatch):
        monkeypatch.chdir(ROOT)
        import yaml

        source = yaml.safe_load(Path(SPEC).read_text())
        for metric in source["acceptance"]["metrics"]:
            metric["target"] = 0.999
        harder = workdir / "harder.yaml"
        harder.write_text(yaml.safe_dump(source), encoding="utf-8")

        code, out, _ = run(
            [*CLOCK, "--workspace", str(workdir / "ws2"), "run", str(harder)], capsys
        )
        assert code == EXIT_ACCEPTANCE_FAILED
        assert "[FAIL]" in out


class TestRtm:
    def test_prints_the_matrix(self, capsys):
        code, out, _ = run(["rtm", SPEC], capsys)
        assert code == EXIT_OK
        assert "Requirements to test traceability" in out
        assert "URS-01" in out

    def test_csv_export(self, capsys):
        code, out, _ = run(["rtm", SPEC, "--csv"], capsys)
        assert code == EXIT_OK
        assert out.startswith("requirement_id,")


class TestPackage:
    def test_writes_the_package_and_reports_why_it_is_not_validated(
        self, capsys, workdir, monkeypatch
    ):
        monkeypatch.chdir(ROOT)
        out_dir = workdir / "pkg"
        code, out, _ = run(
            [*CLOCK, "--workspace", str(workdir / "ws"), "package", SPEC, "-o", str(out_dir)],
            capsys,
        )
        # Unsigned, so not validated: exit 1 with the reason stated.
        assert code == EXIT_ACCEPTANCE_FAILED
        assert (out_dir / "OQ_REPORT.md").exists()
        assert (out_dir / "CREDIBILITY_REPORT.md").exists()
        assert "Satisfied" in out
        assert "Not yet validated because" in out
        assert "lack the required approvals" in out

    def test_outstanding_conditions_are_shown(self, capsys, workdir, monkeypatch):
        monkeypatch.chdir(ROOT)
        _, out, _ = run(
            [
                *CLOCK, "--workspace", str(workdir / "ws3"),
                "package", SPEC, "-o", str(workdir / "pkg3"),
            ],
            capsys,
        )
        assert "Outstanding conditions" in out
        assert "unscripted test" in out

    def test_html_is_written_when_requested(self, capsys, workdir, monkeypatch):
        monkeypatch.chdir(ROOT)
        out_dir = workdir / "pkg2"
        run(
            [
                *CLOCK, "--workspace", str(workdir / "ws2"),
                "package", SPEC, "-o", str(out_dir), "--html",
            ],
            capsys,
        )
        html = (out_dir / "VSR.html").read_text()
        assert html.startswith("<!doctype html>")


class TestAuditAndVerify:
    def _workspace(self, workdir, capsys, monkeypatch):
        monkeypatch.chdir(ROOT)
        workspace = workdir / "ws"
        run([*CLOCK, "--workspace", str(workspace), "run", SPEC], capsys)
        return workspace

    def test_audit_verify_on_a_clean_chain(self, capsys, workdir, monkeypatch):
        workspace = self._workspace(workdir, capsys, monkeypatch)
        code, out, _ = run(["--workspace", str(workspace), "audit", "--verify"], capsys)
        assert code == EXIT_OK
        assert "INTACT" in out

    def test_audit_verify_on_a_broken_chain_exits_three(self, capsys, workdir, monkeypatch):
        import sqlite3

        workspace = self._workspace(workdir, capsys, monkeypatch)
        connection = sqlite3.connect(workspace / "audit.sqlite")
        connection.execute("DROP TRIGGER audit_log_no_update")
        connection.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2")
        connection.commit()
        connection.close()

        code, out, _ = run(["--workspace", str(workspace), "audit", "--verify"], capsys)
        assert code == EXIT_INTEGRITY
        assert "BROKEN" in out

    def test_verify_checks_both_stores(self, capsys, workdir, monkeypatch):
        workspace = self._workspace(workdir, capsys, monkeypatch)
        code, out, _ = run(["--workspace", str(workspace), "verify"], capsys)
        assert code == EXIT_OK
        assert "Audit chain     INTACT" in out
        assert "Evidence vault  INTACT" in out

    def test_verify_exits_three_on_corruption(self, capsys, workdir, monkeypatch):
        import stat

        workspace = self._workspace(workdir, capsys, monkeypatch)
        objects = list((workspace / "vault" / "objects").rglob("*"))
        target = next(p for p in objects if p.is_file())
        target.chmod(stat.S_IWUSR | stat.S_IRUSR)
        target.write_bytes(b"corrupted")

        code, out, _ = run(["--workspace", str(workspace), "verify"], capsys)
        assert code == EXIT_INTEGRITY
        assert "BROKEN" in out

    def test_audit_missing_trail_is_a_usage_error(self, capsys, workdir):
        code, _, err = run(["--workspace", str(workdir / "nothing"), "audit"], capsys)
        assert code == EXIT_USAGE
        assert "no audit trail" in err


class TestSigningCredentials:
    """A password must never be accepted on the command line."""

    def test_no_subcommand_accepts_a_password_argument(self):
        parser = build_parser()
        rendered = parser.format_help()
        subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices][-1]
        for name, sub in subparsers.choices.items():
            options = {
                option
                for action in sub._actions
                for option in action.option_strings
            }
            assert "--password" not in options, name
            assert "--secret" not in options, name

    def test_non_interactive_signing_without_the_environment_is_refused(
        self, capsys, workdir, monkeypatch
    ):
        document = workdir / "doc.md"
        document.write_text("# A document\n", encoding="utf-8")
        monkeypatch.delenv("VALKIT_SIGNING_PASSWORD", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        code, _, err = run(
            [
                *CLOCK, "--workspace", str(workdir / "ws"),
                "sign", str(document), "--user", "qa_lead",
                "--printed-name", "Dana Okafor",
            ],
            capsys,
        )
        assert code == EXIT_USAGE
        assert "never be passed as a command-line argument" in err

    def test_signing_via_the_environment_produces_a_manifest(
        self, capsys, workdir, monkeypatch
    ):
        document = workdir / "doc.md"
        document.write_text("# A document\n", encoding="utf-8")
        monkeypatch.setenv("VALKIT_SIGNING_PASSWORD", "a-strong-password")

        code, out, _ = run(
            [
                *CLOCK, "--workspace", str(workdir / "ws"),
                "sign", str(document), "--user", "qa_lead",
                "--printed-name", "Dana Okafor", "--meaning", "approved",
            ],
            capsys,
        )
        assert code == EXIT_OK
        assert "Dana Okafor" in out
        assert "Approved" in out
        assert "a-strong-password" not in out

    def test_signing_in_place_appends_the_manifest(self, capsys, workdir, monkeypatch):
        document = workdir / "doc.md"
        document.write_text("# A document\n", encoding="utf-8")
        monkeypatch.setenv("VALKIT_SIGNING_PASSWORD", "a-strong-password")

        run(
            [
                *CLOCK, "--workspace", str(workdir / "ws"),
                "sign", str(document), "--user", "qa_lead",
                "--printed-name", "Dana Okafor", "--in-place",
            ],
            capsys,
        )
        content = document.read_text()
        assert "Electronic signature" in content
        assert "a-strong-password" not in content


class TestInvokedAsAProcess:
    def test_the_console_entry_point_works(self):
        result = subprocess.run(
            [sys.executable, "-m", "valkit.cli", "--version"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0
        assert "valkit" in result.stdout

    def test_help_is_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "valkit.cli", "--help"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0
        assert "Exit codes" in result.stdout
