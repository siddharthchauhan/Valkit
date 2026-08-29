# Validating the validator

Every quality function asks this, and they are right to. ValKit produces
evidence supporting a regulated decision, so it is itself software used in a GxP
process. This document sets out what assurance stands behind it and what remains
yours.

## Categorisation

Under GAMP 5 2nd edition, **ValKit as deployed is category 4 (configured)**. You
supply a specification, qualification data, acceptance criteria and a choice of
scorers; you do not alter the tool's logic.

Where you write a custom scorer or a custom model provider, that component is
**category 5 (bespoke)** and is yours to qualify. The extension points are
deliberately narrow — a scorer is one function returning a `Score`, a provider
one method returning a `ProviderResponse` — so the boundary is easy to draw and
easy to argue.

The agents ValKit evaluates are categorised separately, and are typically
category 5.

## Assurance that ships with the tool

| Control | Form |
| --- | --- |
| Numerical core | Pure Python, no third-party numerical library. Small enough to read end to end |
| Statistical correctness | Verified against `mpmath` at 40 decimal digits, and against published interval values |
| Sizing correctness | Every sample-size result checked to be the smallest n that works, by confirming n − 1 fails |
| Evidence integrity | Content-addressed storage; the identifier is the digest; reads re-derive it |
| Audit trail | Hash-chained and append-only, with the digest schema documented so a third party can reimplement verification |
| Tamper detection | Tested by attacking the store directly with the triggers dropped, including a forged record with a recomputed digest |
| Electronic signatures | 21 CFR 11.50, 11.70 and 11.200 implemented clause by clause and tested at their boundaries |
| Credential containment | A test sweeps every surface a password could escape through |
| Reproducibility | A run repeated with the same inputs produces byte-identical records and documents |
| Determinism of documents | The same records and clock render byte-identical output |

## Dependency inventory

The core has two runtime dependencies, deliberately.

| Dependency | Purpose | Notes for a supplier assessment |
| --- | --- | --- |
| PyYAML | Parsing the specification | Widely used, stable interface. `yaml.safe_load` only; no arbitrary object construction. No network access |
| Jinja2 | Rendering documents | Widely used. Rendering is from templates shipped with the package, with `StrictUndefined`. No network access |

Optional extras — FastAPI, uvicorn, boto3, python-docx, the MCP SDK — are
imported lazily inside the functions that need them. A deployment that does not
install them does not carry their supplier risk, and `import valkit` works
without any of them. `mpmath` appears only in the test suite as an independent
cross-check and is never imported at runtime.

## Installation qualification of ValKit itself

Record, for the installed instance:

1. The version installed, and the digest of the distribution.
2. The Python version and platform.
3. The full test suite output. `python -m pytest -q` executes it; every test is
   offline and deterministic.
4. The result of `valkit verify`, which checks the audit chain and the evidence
   vault of a deployment.
5. The dependency versions actually resolved (`pip freeze`).

The demonstration in `examples/demo.py` doubles as an operational qualification
of the tool: it exercises the whole path from specification to signed package
with known inputs and known expected outputs, offline, and its result is
reproducible.

## Release and change process

ValKit's own changes are managed in version control with a test suite that must
pass. A customer relying on it should pin a version, record the digest, and
treat an upgrade as a change requiring re-qualification of anything the upgrade
touches — the same discipline the tool asks of you for your agents.

## What remains yours

These cannot be delegated to a tool, and a vendor who suggests otherwise is
selling you a problem:

- **The context-of-use determination.** ValKit applies documented rules to what
  you declare. A context of use that understates how the output is really used
  produces a risk assessment that understates the risk, and nothing in the tool
  can detect that.
- **Curating the qualification set.** The statistical argument assumes the cases
  are independent and representative. That is a property of how you built the
  set, not of the arithmetic.
- **Deciding the acceptance criteria are appropriate** to the decision the agent
  supports.
- **Reviewing every generated document on its merits** before signing it. A
  generated document is a draft for a qualified person to take responsibility
  for.
- **The quality system.** Procedures, training, account governance, periodic
  review, and the predicate-rule compliance that Part 11 sits on top of.
  Specifically, Part 11 requires organisational controls ValKit cannot supply:
  training of personnel (11.10(i)), the written policy holding individuals
  accountable for their signatures (11.10(j)), identity verification and the
  certification to FDA (11.100(b) and (c)), and safeguards against unauthorised
  use (11.300(d)).
- **Determining which regulations apply** to your use.

## Known limitations

Stated because a qualification document that lists only strengths is not one:

- The identity store shipped with ValKit is an in-memory implementation suitable
  for demonstration and small deployments. A regulated deployment substitutes
  one backed by the customer's directory; the protocol is narrow to make that
  straightforward, but the substitution is the customer's work.
- **The API's `X-ValKit-Actor` header is attribution, not authentication.** It
  says who a request claims to be for, and the API records exactly that. Nothing
  in ValKit verifies the claim: a deployment puts an identity provider in front
  and populates the header from the authenticated session. Served without one,
  every audit record is only as trustworthy as the network. This is the single
  most consequential thing to get right when deploying the API, and it is
  entirely outside the tool.
  Signing is the exception, and deliberately: a signature is verified against
  the identity store's components regardless of the header, so a forged header
  cannot produce a signature. It can misattribute a specification ingestion; it
  cannot misattribute an approval.
- **The API keeps its working state in memory.** Specifications, pipelines and
  rendered documents do not survive a restart, and a document must be signed in
  the process that generated it. The durable records — the hash-chained trail and
  the content-addressed evidence — are on disk or in S3 and are unaffected, which
  is the deliberate part of the design; but a deployment that needs a validation
  to span a restart or several instances needs the Postgres persistence in
  `infra/postgres/`, which is specified there and not wired up.
- The local evidence vault enforces write-once semantics through file
  permissions and content addressing. It is not proof against an administrator
  with filesystem access; S3 Object Lock in Compliance mode is, and is the
  intended production configuration.
- Verification of the audit chain proves internal consistency. It detects any
  change made after the fact by anyone who does not rewrite the entire remainder
  of the trail. It does not prove that a wholesale rewrite never happened; only
  publishing the chain digest externally does that, which is what
  `chain_digest()` is for.

## Naming

"ValKit" is a working name used during development. An unrelated operating
company uses the domain valkit.ai. The name must change before any public
release.
