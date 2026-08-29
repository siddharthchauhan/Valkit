# Architecture

## The shape of the problem

A validation package is a chain of reasoning, not a pile of documents. The
user states what they need; the system states how it provides it; the risk
assessment states what could go wrong; the tests state how each of those is
verified; the evidence states what happened when they ran; and the signatures
state who takes responsibility. A package where that chain is unbroken can be
audited. One where it is not cannot, however good the individual documents look.

ValKit's structure follows that chain, and its layering follows a second
constraint: everything that produces a number must be separable from everything
that presents one, so that a reviewer auditing "how did you decide this passed?"
has exactly one function to read.

## System context

```mermaid
graph TB
  Author[Engineer] -->|valkit.yaml| CLI[ValKit CLI / SDK]
  QA[Validation and QA] --> Console[Web console]
  CI[CI pipeline] --> CLI
  Agent[Coding agent] -->|MCP| MCP[ValKit MCP server]

  CLI --> Core[Validation engine]
  Console --> API[HTTP API]
  API --> Core
  MCP --> Core

  Core --> Models[(Model providers: Bedrock, local, fixture)]
  Core --> Vault[(Evidence vault: WORM)]
  Core --> Audit[(Audit trail: hash-chained)]
  Core --> Docs[Generated package]
  Docs --> Export[eQMS export]
```

## Components

```mermaid
graph LR
  subgraph Input
    Spec[spec: loader, risk, derive]
  end
  subgraph Measurement
    Data[evals.dataset] --> Runner[evals.runner]
    Providers[evals.providers] --> Runner
    Scorers[evals.scorers] --> Runner
    Judge[evals.judge] --> Runner
    Runner --> Stats[stats: bounds, acceptance]
  end
  subgraph Evidence
    Vault[(vault)]
    Audit[(audit)]
    Sign[esign]
  end
  subgraph Output
    Trace[trace: graph, RTM]
    Docgen[docgen: templates]
  end
  Spec --> Runner
  Spec --> Trace
  Stats --> Docgen
  Trace --> Docgen
  Runner --> Vault
  Docgen --> Vault
  Docgen --> Sign
  Sign --> Audit
  Runner --> Audit
  Pipeline[pipeline] --> Spec
  Pipeline --> Runner
  Pipeline --> Docgen
  Pipeline --> Sign
```

`valkit.stats` depends on nothing but the standard library. `valkit.spec`,
`valkit.evals`, `valkit.audit`, `valkit.vault` and `valkit.esign` depend on the
domain model and on `stats` where they need it, and not on each other's
internals. `valkit.pipeline` is the only module that knows the order of
operations.

## The validation lifecycle

```mermaid
stateDiagram-v2
  [*] --> ingest_spec
  ingest_spec --> assess_risk
  assess_risk --> derive: requirements, risks, tests
  derive --> load_datasets
  load_datasets --> run_evals: digest verified
  run_evals --> calibrate_judge
  calibrate_judge --> compute_acceptance
  compute_acceptance --> execute_tests
  execute_tests --> generate_docs
  generate_docs --> await_signature
  await_signature --> await_signature: human approval
  await_signature --> seal
  seal --> validated_gate
  validated_gate --> validated: every condition met
  validated_gate --> in_validation: a condition failed
  validated --> monitor
  monitor --> change_control: drift or change
  change_control --> run_evals: re-evaluation in scope
  validated --> [*]
```

The interrupt at `await_signature` is the consequential part of the design. A
pipeline that ran to completion without stopping could not implement a human
approval step, and a human approval step in the middle of an automated process
is a regulatory requirement rather than a UX preference. `run_all()` therefore
stops short of signing, deliberately: a pipeline that signed on the author's
behalf would defeat the purpose of requiring a signature.

## One validation run

```mermaid
sequenceDiagram
  participant U as User
  participant P as Pipeline
  participant D as Dataset
  participant M as Model provider
  participant S as Stats
  participant G as DocGen
  participant V as Vault
  participant A as Audit

  U->>P: valkit.yaml
  P->>A: validation.opened
  P->>P: assess risk, derive requirements and tests
  P->>D: load, verify pinned digest
  D-->>P: dataset + both digests
  P->>A: run.started (model, dataset digest, harness digest)
  loop each case
    P->>M: prompt (PHI cases routed locally)
    M-->>P: output
    P->>P: apply scorers
  end
  P->>S: k of n per metric
  S-->>P: lower bounds and verdicts
  P->>A: run.metric_evaluated (per metric)
  P->>V: transcripts
  P->>G: records
  G->>V: documents
  G-->>P: document set
  U->>P: sign (all components, first in session)
  P->>A: document.signed
  P->>V: evidence manifest
  P->>P: readiness: every condition, no short-circuit
  P-->>U: VALIDATED, or the conditions that failed
```

## Data model

```mermaid
erDiagram
  AGENT_SPEC ||--|| CONTEXT_OF_USE : declares
  AGENT_SPEC ||--o{ METRIC_SPEC : "acceptance criteria"
  AGENT_SPEC ||--o{ REQUIREMENT : derives
  REQUIREMENT ||--o{ REQUIREMENT : "FRS implements URS"
  RISK }o--o{ REQUIREMENT : threatens
  TEST_CASE }o--o{ REQUIREMENT : verifies
  TEST_CASE }o--o{ RISK : mitigates
  TEST_EXECUTION }o--|| TEST_CASE : executes
  TEST_EXECUTION }o--|| EVAL_RUN : "recorded in"
  TEST_EXECUTION ||--o{ DEVIATION : records
  TEST_EXECUTION }o--o{ EVIDENCE : "evidenced by"
  EVAL_RUN ||--o{ SAMPLE_RESULT : contains
  EVAL_RUN ||--o{ METRIC_RESULT : produces
  EVAL_RUN ||--o| JUDGE_CALIBRATION : includes
  DOCUMENT }o--|| EVAL_RUN : reports
  DOCUMENT }o--o{ EVIDENCE : cites
  DOCUMENT ||--o{ SIGNATURE : "signed by"
  CHANGE_CONTROL }o--o{ EVAL_RUN : "verified by"
  AUDIT_RECORD ||--|| AUDIT_RECORD : "chains to previous"
```

Every edge in the traceability graph is derived from a field one of these
records already carries. Nothing invents a relationship, so the graph is a view
of the package rather than a second source of truth free to drift from it.

## Deployment

```mermaid
graph TB
  subgraph Multi-tenant SaaS
    ALB[Load balancer] --> API[API and workers on Fargate]
    API --> RDS[(RDS PostgreSQL, encrypted)]
    API --> S3[(S3 Object Lock, Compliance mode)]
    API --> KMS[KMS, per-tenant keys]
    EB[EventBridge] -->|scheduled re-evaluation| API
    API --> Bedrock[Bedrock]
  end

  subgraph Single-tenant VPC
    API2[API and workers] --> RDS2[(RDS in customer VPC)]
    API2 --> S32[(S3 Object Lock in customer account)]
    API2 --> Local[Local model on EC2, for PHI]
  end
```

Two deployment shapes, because the customers differ. A vendor validating its own
AI features is well served by multi-tenant SaaS. A sponsor whose qualification
data contains protected health information generally is not, and a
single-tenant VPC deployment with a local model for PHI-bearing cases is the
configuration that makes the data-processing position tractable.

The evidence bucket is never public, and the eval workers need egress to the
model provider while the bucket does not. See `infra/` for the detail, including
the constraint that catches people out: **S3 Object Lock cannot be enabled on an
existing bucket**, so it belongs in the plan from the beginning.

## MCP topology

```mermaid
graph LR
  Editor[Coding agent in an editor] -->|stdio| MCP[ValKit MCP server]
  MCP --> Ingest[valkit.ingest_spec]
  MCP --> Run[valkit.run_evals]
  MCP --> Accept[valkit.compute_acceptance]
  MCP --> Docs[valkit.generate_docs]
  MCP --> Sign[valkit.sign]
  MCP --> Drift[valkit.get_drift]
  MCP --> CC[valkit.open_change_control]
  Ingest --> Core[Validation engine]
  Run --> Core
  Accept --> Core
  Docs --> Core
  Sign --> Core
  Drift --> Core
  CC --> Core
```

The point of the MCP surface is that an engineer's own coding agent can drive a
validation from inside their editor, so that producing evidence is part of
building the agent rather than a separate project afterwards. Credentials passed
to `valkit.sign` are never echoed in a tool result, since a tool result is model
context and may be persisted in a transcript.

The handlers are plain functions of a context object rather than methods on a
server, so the surface can be tested without a transport and the registry does
not depend on the MCP SDK. Arguments are checked against a JSON Schema subset
before a handler runs, and a ValKit error becomes a structured result rather
than a transport failure — a coding agent can act on `"the target must be less
than 1"` and cannot act on a broken connection.

## The four entry points, and what they share

```mermaid
graph TB
  CLI[valkit CLI] --> Shared
  API[HTTP API and console] --> Shared
  MCP[MCP tools] --> Shared
  Worker[Re-evaluation worker] --> Shared

  subgraph Shared[Shared engine]
    Providers["evals.providers.provider_for_spec"]
    Pipeline["pipeline: stages, derive_executions, readiness"]
    Stores[("audit + vault")]
  end

  Shared --> Package[Signed validation package]
```

Four ways in, one engine. That matters more than it looks: a qualification
report generated through a tool call and one generated through the CLI have to
be the same regulatory record, so the derivations that produce them —
`provider_for_spec`, `derive_executions`, `readiness` — live in one place and
every entry point calls them. If they could diverge, the evidence would depend
on the route taken to produce it, which is exactly the property a validation
package cannot have.

The entry points differ only in what they are allowed to do:

| | Runs a battery | Generates documents | Signs | Grants validated status |
| --- | --- | --- | --- | --- |
| CLI | yes | yes | yes, interactively | via the gate |
| HTTP API | yes | yes | yes, one document at a time | via the gate |
| MCP | yes | yes | yes, credential in the call | via the gate |
| Worker | yes | no | **never** | **never** |

The worker's row is the deliberate one. It produces evidence and opens change
controls; it cannot sign and cannot restore validated status. A worker that
could sign could grant validated status to an agent no human had looked at,
which is the opposite of what the signature is for.

## Monitoring and re-evaluation

```mermaid
sequenceDiagram
  participant S as Scheduler (EventBridge or the loop)
  participant W as Worker
  participant A as Audit
  participant V as Vault
  participant M as Drift monitor
  participant C as Change control

  S->>W: pass
  W->>A: verify chain
  W->>V: verify evidence
  alt either fails
    W-->>S: exit 3, no evidence produced
  else both intact
    loop each specification
      W->>W: due? (cron vs. last observation)
      W->>W: run the battery
      W->>M: record one point per metric
      M->>M: control limits from history
      alt a rule trips
        M->>C: open a change control
        M-->>W: alert
      end
      W->>A: monitoring.reevaluated
    end
    W-->>S: exit 1 if alerted, else 0
  end
```

Integrity is checked before anything is produced, not alongside the results.
Appending new evidence to a chain that does not verify would extend a record
nobody can rely on, and the exit code is what the deployment's alarms watch.

## Decisions worth knowing

**The core depends on PyYAML and Jinja2, and nothing else.** Every third-party
package in a GxP tool is a supplier that has to be assessed. The statistics are
implemented from published algorithms in pure Python so the numerical core can
be qualified by inspection. Optional extras are imported lazily and their
absence raises a clear ValKit error rather than an ImportError.

**Time is injected.** Everything takes a `Clock`. A validation artefact that
cannot be regenerated byte-for-byte cannot be compared against the signed
version, so reproducibility is a structural property rather than a discipline.

**Content addressing rather than checksums.** Evidence lives at a path derived
from the SHA-256 of its bytes, so an identifier that resolves is itself proof of
integrity, writes are idempotent, and overwriting is impossible by construction
rather than merely forbidden.

**Guard rails are named as guard rails.** The SQLite triggers on the audit table
and the read-only file permissions in the vault stop accidents and can be
demonstrated to an inspector, but anyone who can drop a trigger can bypass one.
The hash chain and the content addressing are the actual controls, and the
docstrings say which is which rather than letting a reader infer more assurance
than exists.

**Strictness at the boundaries.** Unknown keys in a specification are rejected;
an undefined template variable raises; a document whose required inputs are
absent is not generated. In ordinary software these would be unfriendly. In a
system whose output gets signed, a silent default is a defect nobody sees.
