"""The requirements-to-test traceability matrix.

The RTM is the document an auditor opens first, and the row they look for is the
one that is missing. This module therefore renders the gaps as prominently as
the coverage: the "requirements with no verifying test" section is always
present and says "None" when it is empty, rather than being omitted. An absent
heading looks like an absent question.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Sequence

from ..models import Document, EvalRun, Requirement, Risk, TestCase, TestExecution
from .graph import Coverage, TraceabilityGraph

__all__ = ["RtmRow", "build_rtm", "render_markdown", "render_csv", "render_compact", "natural_key"]


@dataclass
class RtmRow:
    """One row of the matrix: a requirement and everything that verifies it."""

    requirement_id: str
    text: str
    kind: str
    critical: bool
    risks: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    verdict: str = "no test"
    """One of: verified, not verified, not executed, no test."""

    @property
    def satisfied(self) -> bool:
        return self.verdict == "verified"


_NATURAL_PATTERN = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key placing URS-2 before URS-10 rather than after it."""
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in _NATURAL_PATTERN.split(text)
        if part
    )


def build_rtm(
    graph: TraceabilityGraph | None = None,
    *,
    requirements: Sequence[Requirement] = (),
    risks: Sequence[Risk] = (),
    tests: Sequence[TestCase] = (),
    executions: Sequence[TestExecution] = (),
    runs: Sequence[EvalRun] = (),
    documents: Sequence[Document] = (),
) -> list[RtmRow]:
    """Build the matrix, either from a graph or from the records directly."""
    if graph is None:
        graph = TraceabilityGraph.from_records(
            requirements=requirements,
            risks=risks,
            tests=tests,
            executions=executions,
            runs=runs,
            documents=documents,
        )

    execution_status = {
        node.node_id: bool(node.attributes.get("passed"))
        for node in graph.nodes_of("execution")
    }

    rows: list[RtmRow] = []
    for requirement in graph.nodes_of("requirement"):
        req_id = requirement.node_id
        linked_tests = sorted(set(graph.sources_of("requirement", req_id, "verifies")), key=natural_key)
        linked_risks = sorted(set(graph.sources_of("requirement", req_id, "threatens")), key=natural_key)

        row_executions: list[str] = []
        row_runs: list[str] = []
        row_evidence: list[str] = []
        for test_id in linked_tests:
            for execution_id in graph.sources_of("test", test_id, "executes"):
                row_executions.append(execution_id)
                row_runs.extend(graph.targets_of("execution", execution_id, "recorded_in"))
                row_evidence.extend(graph.targets_of("execution", execution_id, "evidenced_by"))

        row_documents: list[str] = []
        for run_id in set(row_runs):
            row_documents.extend(graph.sources_of("run", run_id, "reports"))

        if not linked_tests:
            verdict = "no test"
        elif not row_executions:
            verdict = "not executed"
        elif all(execution_status.get(execution_id, False) for execution_id in row_executions):
            verdict = "verified"
        else:
            verdict = "not verified"

        rows.append(
            RtmRow(
                requirement_id=req_id,
                text=requirement.label,
                kind=str(requirement.attributes.get("kind", "")),
                critical=requirement.critical,
                risks=linked_risks,
                tests=linked_tests,
                executions=sorted(set(row_executions), key=natural_key),
                runs=sorted(set(row_runs), key=natural_key),
                evidence=sorted(set(row_evidence)),
                documents=sorted(set(row_documents), key=natural_key),
                verdict=verdict,
            )
        )

    return sorted(rows, key=lambda row: natural_key(row.requirement_id))


def _fraction(numerator: int, denominator: int) -> str:
    """Render a ratio, refusing to report nought out of nought as complete.

    A coverage table that shows "100%" for a measure with nothing to measure
    reads as evidence of completeness, which is exactly the impression a
    validation document must not give when the work has not been done.
    """
    if denominator == 0:
        return f"{numerator} of 0 (not applicable)"
    return f"{numerator} of {denominator} ({numerator / denominator:.0%})"


def _truncate(text: str, limit: int = 90) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _cell(values: Sequence[str], limit: int = 4) -> str:
    if not values:
        return "—"
    shown = ", ".join(values[:limit])
    return shown if len(values) <= limit else f"{shown} (+{len(values) - limit})"


def _short(values: Sequence[str], limit: int = 2, width: int = 12) -> str:
    """Abbreviate digests, which are unreadable and unhelpful at full length."""
    if not values:
        return "—"
    shown = ", ".join(f"{value[:width]}…" if len(value) > width else value for value in values[:limit])
    return shown if len(values) <= limit else f"{shown} (+{len(values) - limit})"


def render_markdown(
    rows: Sequence[RtmRow], coverage: Coverage | None = None, include_summary: bool = True
) -> str:
    """Render the matrix as it appears in the RTM document."""
    lines: list[str] = []

    if include_summary and coverage is not None:
        lines.extend(
            [
                "### Coverage summary",
                "",
                "| Measure | Value |",
                "| --- | --- |",
                f"| Requirements | {coverage.requirements_covered} of "
                f"{coverage.requirements_total} verified "
                f"({coverage.requirement_coverage:.0%}) |",
                f"| Critical requirements | {coverage.critical_covered} of "
                f"{coverage.critical_total} verified "
                f"({coverage.critical_coverage:.0%}) |",
                f"| Risks with a verifying test | {coverage.risks_mitigated} of "
                f"{coverage.risks_total} ({coverage.risk_coverage:.0%}) |",
                f"| Tests executed | {_fraction(coverage.tests_executed, coverage.tests_total)} |",
                f"| Executions with evidence | "
                f"{_fraction(coverage.executions_with_evidence, coverage.executions_total)} |",
                "",
            ]
        )

    lines.extend(
        [
            "### Requirements to test traceability",
            "",
            "| Requirement | Type | Critical | Risks | Tests | Runs | Evidence | Verdict |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            f"| **{row.requirement_id}** {_truncate(row.text, 70)} "
            f"| {row.kind} "
            f"| {'Yes' if row.critical else 'No'} "
            f"| {_cell(row.risks)} "
            f"| {_cell(row.tests)} "
            f"| {_cell(row.runs, 2)} "
            f"| {_short(row.evidence)} "
            f"| {row.verdict} |"
        )

    gaps = [row for row in rows if row.verdict == "no test"]
    critical_gaps = [row for row in gaps if row.critical]
    lines.extend(["", "### Requirements with no verifying test", ""])
    if not gaps:
        lines.append("None. Every requirement is linked to at least one test.")
    else:
        for row in gaps:
            marker = "**CRITICAL** " if row.critical else ""
            lines.append(f"- {marker}{row.requirement_id}: {_truncate(row.text)}")
        if critical_gaps:
            lines.extend(
                [
                    "",
                    f"{len(critical_gaps)} of these are critical. A critical requirement "
                    "with no verifying test is not validated, and the validation package "
                    "is incomplete until each is either covered by a test or reclassified "
                    "with a documented rationale.",
                ]
            )

    unverified = [row for row in rows if row.verdict in ("not verified", "not executed")]
    lines.extend(["", "### Requirements not demonstrated", ""])
    if not unverified:
        lines.append("None. Every linked test was executed and passed.")
    else:
        for row in unverified:
            lines.append(
                f"- {row.requirement_id} ({row.verdict}): tests {_cell(row.tests)}"
            )

    return "\n".join(lines)


def render_csv(rows: Sequence[RtmRow]) -> str:
    """Export for a customer's existing validation system."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "requirement_id", "text", "kind", "critical", "risks", "tests",
            "executions", "runs", "evidence", "documents", "verdict",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.requirement_id,
                " ".join(row.text.split()),
                row.kind,
                "yes" if row.critical else "no",
                "; ".join(row.risks),
                "; ".join(row.tests),
                "; ".join(row.executions),
                "; ".join(row.runs),
                "; ".join(row.evidence),
                "; ".join(row.documents),
                row.verdict,
            ]
        )
    return buffer.getvalue()


def render_compact(rows: Sequence[RtmRow]) -> str:
    """The one-line chain form, for a summary section or a commit message."""
    lines: list[str] = []
    for row in rows:
        parts = [row.requirement_id]
        if row.risks:
            parts.append(row.risks[0] if len(row.risks) == 1 else f"{row.risks[0]}+{len(row.risks) - 1}")
        if row.tests:
            parts.append(row.tests[0] if len(row.tests) == 1 else f"{row.tests[0]}+{len(row.tests) - 1}")
        if row.runs:
            parts.append(row.runs[0])
        if row.evidence:
            parts.append(f"EVID {row.evidence[0][:12]}…")
        parts.append(f"[{row.verdict}]")
        lines.append(" -> ".join(parts))
    return "\n".join(lines)
