"""The traceability graph.

GAMP 5's central discipline is that nothing in a validation package stands
alone: every requirement traces to the risk that motivates it, the test that
verifies it, the run that executed the test, the evidence that run produced,
and the document section that states the conclusion. A package where that chain
is unbroken can be audited; one where it is not cannot, however good the
individual documents look.

The most useful thing this module does is therefore not to draw the chain but
to find where it breaks. :meth:`TraceabilityGraph.validate` reports orphaned
requirements, unmitigated risks, tests that were never executed and documents
with no supporting evidence, each as a typed finding naming the identifiers
involved. An uncovered requirement is the commonest audit finding against a
traceability matrix, and a tool that produced one silently would be worse than
no tool.

Findings are separated into those that block a validation package and those
that merely warrant attention, because treating every gap as fatal trains
people to ignore the report.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..models import (
    ChangeControl,
    Document,
    EvalRun,
    EvidenceRecord,
    Requirement,
    Risk,
    Signature,
    TestCase,
    TestExecution,
    TraceLink,
)

__all__ = [
    "TraceNode",
    "TraceFinding",
    "TraceValidation",
    "Coverage",
    "TraceabilityGraph",
    "NODE_TYPES",
]

NODE_TYPES = (
    "requirement",
    "risk",
    "test",
    "execution",
    "run",
    "evidence",
    "document",
    "signature",
    "change_control",
)


@dataclass(frozen=True)
class TraceNode:
    node_type: str
    node_id: str
    label: str = ""
    critical: bool = False
    attributes: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.node_type, self.node_id)


@dataclass(frozen=True)
class TraceFinding:
    """One defect in the traceability chain."""

    kind: str
    severity: str
    """``blocking`` prevents a validation package; ``advisory`` does not."""

    message: str
    node_ids: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"


@dataclass(frozen=True)
class TraceValidation:
    ok: bool
    findings: list[TraceFinding] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    @property
    def blocking(self) -> list[TraceFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def advisory(self) -> list[TraceFinding]:
        return [f for f in self.findings if not f.blocking]

    def of_kind(self, kind: str) -> list[TraceFinding]:
        return [f for f in self.findings if f.kind == kind]


@dataclass(frozen=True)
class Coverage:
    """Coverage of the validation package, as exact fractions and percentages."""

    requirements_total: int
    requirements_covered: int
    critical_total: int
    critical_covered: int
    risks_total: int
    risks_mitigated: int
    tests_total: int
    tests_executed: int
    executions_with_evidence: int
    executions_total: int

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return 1.0 if denominator == 0 else numerator / denominator

    @property
    def requirement_coverage(self) -> float:
        return self._ratio(self.requirements_covered, self.requirements_total)

    @property
    def critical_coverage(self) -> float:
        return self._ratio(self.critical_covered, self.critical_total)

    @property
    def risk_coverage(self) -> float:
        return self._ratio(self.risks_mitigated, self.risks_total)

    @property
    def execution_rate(self) -> float:
        return self._ratio(self.tests_executed, self.tests_total)

    @property
    def evidence_completeness(self) -> float:
        return self._ratio(self.executions_with_evidence, self.executions_total)

    @property
    def complete(self) -> bool:
        """A package is complete only at full critical-requirement coverage.

        Non-critical requirements may legitimately go unverified, with the
        rationale recorded. A critical one may not.
        """
        return self.critical_covered == self.critical_total


class TraceabilityGraph:
    """A typed directed graph over the artefacts of a validation package."""

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], TraceNode] = {}
        self._out: dict[tuple[str, str], list[TraceLink]] = defaultdict(list)
        self._in: dict[tuple[str, str], list[TraceLink]] = defaultdict(list)
        self._links: list[TraceLink] = []

    # -- construction ------------------------------------------------------

    def add_node(self, node: TraceNode) -> None:
        self._nodes[node.key] = node

    def add_link(self, link: TraceLink) -> None:
        self._links.append(link)
        self._out[(link.source_type, link.source_id)].append(link)
        self._in[(link.target_type, link.target_id)].append(link)

    def link(
        self, source_type: str, source_id: str, relation: str, target_type: str, target_id: str
    ) -> None:
        self.add_link(TraceLink(source_type, source_id, relation, target_type, target_id))

    @classmethod
    def from_records(
        cls,
        *,
        requirements: Sequence[Requirement] = (),
        risks: Sequence[Risk] = (),
        tests: Sequence[TestCase] = (),
        executions: Sequence[TestExecution] = (),
        runs: Sequence[EvalRun] = (),
        evidence: Sequence[EvidenceRecord] = (),
        documents: Sequence[Document] = (),
        change_controls: Sequence[ChangeControl] = (),
    ) -> "TraceabilityGraph":
        """Build the graph from the identifier lists already on the records.

        Nothing here invents a relationship: every edge comes from a field a
        record already carries, so the graph is a view of the package rather
        than a second source of truth that could drift from it.
        """
        graph = cls()

        for requirement in requirements:
            graph.add_node(
                TraceNode(
                    "requirement",
                    requirement.req_id,
                    requirement.text,
                    critical=requirement.critical,
                    attributes={"kind": requirement.kind.value, "source": requirement.source},
                )
            )
        for requirement in requirements:
            for parent in requirement.parent_ids:
                graph.link("requirement", requirement.req_id, "implements", "requirement", parent)

        for risk in risks:
            graph.add_node(
                TraceNode(
                    "risk",
                    risk.risk_id,
                    risk.description,
                    attributes={"class": risk.risk_class.value, "category": risk.category},
                )
            )
            for req_id in risk.requirement_ids:
                graph.link("risk", risk.risk_id, "threatens", "requirement", req_id)

        for test in tests:
            graph.add_node(
                TraceNode(
                    "test",
                    test.test_id,
                    test.title,
                    attributes={"phase": test.phase.value, "scripted": test.scripted},
                )
            )
            for req_id in test.requirement_ids:
                graph.link("test", test.test_id, "verifies", "requirement", req_id)
            for risk_id in test.risk_ids:
                graph.link("test", test.test_id, "mitigates", "risk", risk_id)

        for execution in executions:
            execution_id = f"{execution.test_id}@{execution.run_id}"
            graph.add_node(
                TraceNode(
                    "execution",
                    execution_id,
                    f"{execution.test_id} executed in {execution.run_id}",
                    attributes={"passed": execution.passed, "executed_at": execution.executed_at},
                )
            )
            graph.link("execution", execution_id, "executes", "test", execution.test_id)
            graph.link("execution", execution_id, "recorded_in", "run", execution.run_id)
            for ref in execution.evidence_refs:
                graph.link("execution", execution_id, "evidenced_by", "evidence", ref)

        for run in runs:
            graph.add_node(
                TraceNode(
                    "run",
                    run.run_id,
                    f"{run.agent_id}@{run.agent_version}",
                    attributes={"status": run.status.value, "passed": run.passed},
                )
            )
            if run.transcripts_ref:
                graph.link("run", run.run_id, "produced", "evidence", run.transcripts_ref)

        for record in evidence:
            graph.add_node(
                TraceNode(
                    "evidence",
                    record.evidence_id,
                    f"{record.kind} ({record.size_bytes} bytes)",
                    attributes={"kind": record.kind, "uri": record.uri},
                )
            )

        for document in documents:
            graph.add_node(
                TraceNode(
                    "document",
                    document.doc_id,
                    document.title,
                    attributes={"type": document.doc_type.value, "status": document.status.value},
                )
            )
            if document.run_id:
                graph.link("document", document.doc_id, "reports", "run", document.run_id)
            for ref in document.evidence_refs:
                graph.link("document", document.doc_id, "cites", "evidence", ref)
            for signature in document.signatures:
                graph.add_node(
                    TraceNode(
                        "signature",
                        signature.signature_id,
                        f"{signature.printed_name} ({signature.meaning.value})",
                        attributes={"signer": signature.signer_id, "at": signature.signed_at},
                    )
                )
                graph.link(
                    "signature", signature.signature_id, "signs", "document", document.doc_id
                )

        for change in change_controls:
            graph.add_node(
                TraceNode(
                    "change_control",
                    change.cc_id,
                    change.reason,
                    attributes={"status": change.status.value, "trigger": change.trigger.value},
                )
            )
            for run_id in change.run_ids:
                graph.link("change_control", change.cc_id, "verified_by", "run", run_id)

        return graph

    # -- queries -----------------------------------------------------------

    @property
    def nodes(self) -> list[TraceNode]:
        return list(self._nodes.values())

    @property
    def links(self) -> list[TraceLink]:
        return list(self._links)

    def node(self, node_type: str, node_id: str) -> TraceNode | None:
        return self._nodes.get((node_type, node_id))

    def nodes_of(self, node_type: str) -> list[TraceNode]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def edges_from(self, node_type: str, node_id: str) -> list[TraceLink]:
        return list(self._out.get((node_type, node_id), ()))

    def edges_to(self, node_type: str, node_id: str) -> list[TraceLink]:
        return list(self._in.get((node_type, node_id), ()))

    def sources_of(self, node_type: str, node_id: str, relation: str | None = None) -> list[str]:
        return [
            link.source_id
            for link in self.edges_to(node_type, node_id)
            if relation is None or link.relation == relation
        ]

    def targets_of(self, node_type: str, node_id: str, relation: str | None = None) -> list[str]:
        return [
            link.target_id
            for link in self.edges_from(node_type, node_id)
            if relation is None or link.relation == relation
        ]

    def trace_forward(self, node_type: str, node_id: str, max_depth: int = 8) -> list[list[str]]:
        """Every chain leading away from a node, as readable paths."""
        chains: list[list[str]] = []

        def walk(current: tuple[str, str], path: list[str], depth: int, seen: set) -> None:
            if depth > max_depth or current in seen:
                chains.append(path)
                return
            outgoing = self._in.get(current, [])
            if not outgoing:
                chains.append(path)
                return
            for link in outgoing:
                source = (link.source_type, link.source_id)
                walk(source, [*path, f"{link.source_type}:{link.source_id}"], depth + 1, seen | {current})

        walk((node_type, node_id), [f"{node_type}:{node_id}"], 0, set())
        return chains

    def trace_back(self, node_type: str, node_id: str, max_depth: int = 8) -> list[list[str]]:
        """Every chain leading to a node, for answering 'why does this exist?'."""
        chains: list[list[str]] = []

        def walk(current: tuple[str, str], path: list[str], depth: int, seen: set) -> None:
            if depth > max_depth or current in seen:
                chains.append(path)
                return
            outgoing = self._out.get(current, [])
            if not outgoing:
                chains.append(path)
                return
            for link in outgoing:
                target = (link.target_type, link.target_id)
                walk(target, [*path, f"{link.target_type}:{link.target_id}"], depth + 1, seen | {current})

        walk((node_type, node_id), [f"{node_type}:{node_id}"], 0, set())
        return chains

    # -- validation --------------------------------------------------------

    def validate(self) -> TraceValidation:
        """Find every break in the chain."""
        findings: list[TraceFinding] = []

        dangling = sorted(
            {
                f"{link.target_type}:{link.target_id}"
                for link in self._links
                if (link.target_type, link.target_id) not in self._nodes
            }
        )
        if dangling:
            findings.append(
                TraceFinding(
                    "dangling_reference",
                    "blocking",
                    f"{len(dangling)} reference(s) point at artefacts that do not exist: "
                    f"{', '.join(dangling[:8])}"
                    f"{'...' if len(dangling) > 8 else ''}. The matrix cannot be relied on "
                    "while it refers to things that are not there.",
                    dangling,
                )
            )

        uncovered_critical: list[str] = []
        uncovered_other: list[str] = []
        for requirement in self.nodes_of("requirement"):
            verifiers = self.sources_of("requirement", requirement.node_id, "verifies")
            if verifiers:
                continue
            (uncovered_critical if requirement.critical else uncovered_other).append(
                requirement.node_id
            )

        if uncovered_critical:
            findings.append(
                TraceFinding(
                    "uncovered_requirement",
                    "blocking",
                    f"{len(uncovered_critical)} critical requirement(s) have no verifying "
                    f"test: {', '.join(sorted(uncovered_critical))}. A critical requirement "
                    "that is not verified is not validated.",
                    sorted(uncovered_critical),
                )
            )
        if uncovered_other:
            findings.append(
                TraceFinding(
                    "uncovered_requirement",
                    "advisory",
                    f"{len(uncovered_other)} non-critical requirement(s) have no verifying "
                    f"test: {', '.join(sorted(uncovered_other))}. This is acceptable where "
                    "the rationale is recorded.",
                    sorted(uncovered_other),
                )
            )

        unmitigated = [
            risk.node_id
            for risk in self.nodes_of("risk")
            if not self.sources_of("risk", risk.node_id, "mitigates")
        ]
        if unmitigated:
            findings.append(
                TraceFinding(
                    "unmitigated_risk",
                    "blocking",
                    f"{len(unmitigated)} risk(s) have no test demonstrating their control: "
                    f"{', '.join(sorted(unmitigated))}.",
                    sorted(unmitigated),
                )
            )

        idle_tests = [
            test.node_id
            for test in self.nodes_of("test")
            if not self.targets_of("test", test.node_id, "verifies")
        ]
        if idle_tests:
            findings.append(
                TraceFinding(
                    "test_verifies_nothing",
                    "advisory",
                    f"{len(idle_tests)} test(s) verify no requirement: "
                    f"{', '.join(sorted(idle_tests))}. Either the link is missing or the "
                    "test is not needed.",
                    sorted(idle_tests),
                )
            )

        executed = {
            link.target_id for link in self._links if link.relation == "executes"
        }
        # An unscripted test verifies a control that only exists once the system
        # is in operation - that a reviewer actually reviews, that the schedule
        # actually fires, that periodic review actually happens. Those cannot be
        # demonstrated before operation, so their absence is an outstanding
        # condition to state, not an omission to fail the package for. A
        # scripted test that was never executed is an omission.
        never_run_scripted = sorted(
            t.node_id
            for t in self.nodes_of("test")
            if t.node_id not in executed and t.attributes.get("scripted", True)
        )
        never_run_unscripted = sorted(
            t.node_id
            for t in self.nodes_of("test")
            if t.node_id not in executed and not t.attributes.get("scripted", True)
        )
        if never_run_scripted:
            findings.append(
                TraceFinding(
                    "test_not_executed",
                    "blocking",
                    f"{len(never_run_scripted)} scripted test(s) were never executed: "
                    f"{', '.join(never_run_scripted)}.",
                    never_run_scripted,
                )
            )
        if never_run_unscripted:
            findings.append(
                TraceFinding(
                    "unscripted_test_pending",
                    "advisory",
                    f"{len(never_run_unscripted)} unscripted test(s) remain to be performed "
                    f"against live operation: {', '.join(never_run_unscripted)}. These "
                    f"verify controls that cannot be demonstrated before the system is in "
                    f"use, and validated status is conditional on completing them.",
                    never_run_unscripted,
                )
            )

        unevidenced = [
            execution.node_id
            for execution in self.nodes_of("execution")
            if not self.targets_of("execution", execution.node_id, "evidenced_by")
        ]
        if unevidenced:
            findings.append(
                TraceFinding(
                    "execution_without_evidence",
                    "blocking",
                    f"{len(unevidenced)} test execution(s) have no evidence: "
                    f"{', '.join(sorted(unevidenced))}. A recorded result with no evidence "
                    "is an assertion, not a demonstration.",
                    sorted(unevidenced),
                )
            )

        unsigned = [
            document.node_id
            for document in self.nodes_of("document")
            if document.attributes.get("status") == "approved"
            and not self.sources_of("document", document.node_id, "signs")
        ]
        if unsigned:
            findings.append(
                TraceFinding(
                    "approved_without_signature",
                    "blocking",
                    f"{len(unsigned)} document(s) are marked approved but carry no "
                    f"signature: {', '.join(sorted(unsigned))}.",
                    sorted(unsigned),
                )
            )

        cycle = self._find_cycle()
        if cycle:
            findings.append(
                TraceFinding(
                    "cycle",
                    "advisory",
                    f"the graph contains a cycle: {' -> '.join(cycle)}",
                    cycle,
                )
            )

        return TraceValidation(ok=not any(f.blocking for f in findings), findings=findings)

    def _find_cycle(self) -> list[str]:
        """Return one cycle if the graph has any, using an iterative DFS."""
        colour: dict[tuple[str, str], int] = {}
        parent: dict[tuple[str, str], tuple[str, str] | None] = {}

        for start in self._nodes:
            if colour.get(start, 0) != 0:
                continue
            stack: list[tuple[tuple[str, str], bool]] = [(start, False)]
            parent[start] = None
            while stack:
                node, finished = stack.pop()
                if finished:
                    colour[node] = 2
                    continue
                if colour.get(node, 0) == 1:
                    continue
                colour[node] = 1
                stack.append((node, True))
                for link in self._out.get(node, ()):
                    target = (link.target_type, link.target_id)
                    if target not in self._nodes:
                        continue
                    state = colour.get(target, 0)
                    if state == 1:
                        path = [f"{target[0]}:{target[1]}"]
                        walker: tuple[str, str] | None = node
                        while walker is not None and walker != target:
                            path.append(f"{walker[0]}:{walker[1]}")
                            walker = parent.get(walker)
                        path.append(f"{target[0]}:{target[1]}")
                        return list(reversed(path))
                    if state == 0:
                        parent[target] = node
                        stack.append((target, False))
        return []

    # -- coverage ----------------------------------------------------------

    def coverage(self) -> Coverage:
        requirements = self.nodes_of("requirement")
        critical = [r for r in requirements if r.critical]
        covered = [
            r for r in requirements if self.sources_of("requirement", r.node_id, "verifies")
        ]
        covered_critical = [r for r in covered if r.critical]

        risks = self.nodes_of("risk")
        mitigated = [r for r in risks if self.sources_of("risk", r.node_id, "mitigates")]

        tests = self.nodes_of("test")
        executed = {link.target_id for link in self._links if link.relation == "executes"}
        executions = self.nodes_of("execution")
        with_evidence = [
            e for e in executions if self.targets_of("execution", e.node_id, "evidenced_by")
        ]

        return Coverage(
            requirements_total=len(requirements),
            requirements_covered=len(covered),
            critical_total=len(critical),
            critical_covered=len(covered_critical),
            risks_total=len(risks),
            risks_mitigated=len(mitigated),
            tests_total=len(tests),
            tests_executed=len([t for t in tests if t.node_id in executed]),
            executions_with_evidence=len(with_evidence),
            executions_total=len(executions),
        )

    # -- rendering ---------------------------------------------------------

    def render_mermaid(self, focus: tuple[str, str] | None = None, max_nodes: int = 60) -> str:
        """Render as a Mermaid graph.

        ``focus`` restricts the drawing to the neighbourhood of one node. A
        four-hundred-node matrix rendered whole is unreadable, and an unreadable
        diagram in a validation document is worse than none: it looks like
        evidence without being legible as any.
        """
        included = self._focus_set(focus, max_nodes)

        lines = ["graph LR"]
        shapes = {
            "requirement": ('["', '"]'),
            "risk": ('{{"', '"}}'),
            "test": ('("', '")'),
            "execution": ('>"', '"]'),
            "run": ('[/"', '"/]'),
            "evidence": ('[("', '")]'),
            "document": ('["', '"]'),
            "signature": ('(("', '"))'),
            "change_control": ('{"', '"}'),
        }
        for key in sorted(included):
            node = self._nodes[key]
            open_shape, close_shape = shapes.get(node.node_type, ('["', '"]'))
            label = _escape_mermaid(node.node_id)
            lines.append(f"  {_mermaid_id(key)}{open_shape}{label}{close_shape}")

        for link in self._links:
            source = (link.source_type, link.source_id)
            target = (link.target_type, link.target_id)
            if source in included and target in included:
                lines.append(
                    f"  {_mermaid_id(source)} -->|{link.relation}| {_mermaid_id(target)}"
                )

        if len(included) < len(self._nodes):
            lines.append(
                f"  %% showing {len(included)} of {len(self._nodes)} nodes"
            )
        return "\n".join(lines)

    def _focus_set(
        self, focus: tuple[str, str] | None, max_nodes: int
    ) -> set[tuple[str, str]]:
        if focus is None:
            return set(sorted(self._nodes)[:max_nodes])
        included = {focus}
        frontier = {focus}
        while frontier and len(included) < max_nodes:
            next_frontier: set[tuple[str, str]] = set()
            for node in frontier:
                for link in self._out.get(node, ()):
                    next_frontier.add((link.target_type, link.target_id))
                for link in self._in.get(node, ()):
                    next_frontier.add((link.source_type, link.source_id))
            next_frontier -= included
            for node in sorted(next_frontier):
                if len(included) >= max_nodes:
                    break
                if node in self._nodes:
                    included.add(node)
            frontier = next_frontier & included
        return included

    def to_dot(self) -> str:
        """Render as Graphviz DOT, which validation teams often prefer."""
        lines = ["digraph traceability {", '  rankdir="LR";', '  node [shape=box];']
        for key in sorted(self._nodes):
            node = self._nodes[key]
            lines.append(
                f'  "{node.node_type}:{node.node_id}" [label="{_escape_dot(node.node_id)}"];'
            )
        for link in self._links:
            lines.append(
                f'  "{link.source_type}:{link.source_id}" -> '
                f'"{link.target_type}:{link.target_id}" [label="{link.relation}"];'
            )
        lines.append("}")
        return "\n".join(lines)


def _mermaid_id(key: tuple[str, str]) -> str:
    """A Mermaid-safe node identifier.

    Mermaid identifiers may not contain quotes, spaces or punctuation, and an
    artefact identifier is user-supplied: a requirement id copied from a
    customer's system can contain anything. Everything outside the safe set is
    replaced rather than escaped, since the readable form is the label.
    """
    raw = f"{key[0][:3]}_{key[1]}"
    return "".join(char if char.isalnum() or char == "_" else "_" for char in raw)


def _escape_mermaid(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _escape_dot(text: str) -> str:
    return text.replace('"', '\\"')
