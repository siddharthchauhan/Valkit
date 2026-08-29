# Data protection

## Protected health information

ValKit itself holds no patient data. A customer's qualification set may.

The control is routing. A sample flagged `contains_phi` is sent to the model
named in `models.phi_safe_local` rather than to the primary provider. If
PHI-flagged samples exist and no local provider was supplied, **the run is
refused** — not warned about, refused:

```
65 sample(s) in 'golden_v7' are flagged as containing protected health
information, and the specification names 'ollama/llama3.1:70b' to evaluate them
locally, but no local provider was supplied to the runner. The run is refused:
PHI must not be sent to the default provider.
```

A quiet fallback to the hosted provider would be the worst defect this product
could have, which is why it is a hard failure with an explicit message and is
tested from several directions.

Flagging is by the `contains_phi` field or by any of an overridable set of
metadata keys. The flagging itself is the customer's responsibility: ValKit
routes what it is told about and cannot detect PHI it was not told about.

A practical recommendation: prefer a de-identified qualification set. It removes
the routing question entirely, and a set built from real records is rarely more
representative than one built deliberately.

## Tenant isolation

In the multi-tenant deployment, isolation rests on three controls together:
PostgreSQL row-level security keyed on tenant, per-tenant KMS keys so that a
storage-layer mistake does not become a data-exposure mistake, and separate S3
prefixes with IAM policies scoped to them. Evidence is content-addressed, so an
identifier reveals nothing about its content, but identifiers are still scoped
per tenant.

A customer for whom multi-tenancy is not acceptable — which includes most
sponsors whose data touches PHI — should deploy single-tenant into their own
VPC. That configuration exists for exactly this reason.

## Encryption and residency

At rest: S3 with SSE-KMS, RDS encrypted, KMS key rotation enabled. In transit:
TLS throughout. EU data residency is a deployment variable; the infrastructure
supports pinning every component to a European region.

Model providers are the exception worth naming. Sending a prompt to a hosted
model sends it outside your infrastructure, whatever ValKit does. That is a
contractual question — a business associate agreement, a data processing
agreement, a zero-retention commitment — and it is yours to establish. ValKit
records which provider handled which sample so that the position is at least
auditable.

## Retention and the erasure problem

Evidence is held under a retention policy, defaulting to ten years but properly
set from the customer's own policy. In production the backing store is S3 Object
Lock in **Compliance** mode: for the retention period, no principal can delete
or overwrite the object version, including the account root.

That is the point, and it creates a genuine problem that should be stated rather
than hidden.

**A record under Compliance-mode retention cannot be deleted, including in
response to a data subject's erasure request.** If a qualification set contains
personal data and is sealed into the evidence vault, an erasure request cannot
be satisfied for that copy until retention expires. Governance mode allows a
privileged principal to shorten retention, but it correspondingly weakens the
integrity guarantee that made the store worth using.

There is no way to have both properties. The workable positions are:

1. **De-identify the qualification set** so that no personal data enters the
   vault. This is the recommended route and removes the conflict.
2. **Rely on the regulatory retention obligation** as the lawful basis for
   continued processing, where one genuinely applies. Clinical trial records
   frequently carry such obligations, and erasure rights are not absolute where
   they do. This is a legal determination and needs legal advice.
3. **Use Governance mode** and accept the weaker integrity position, documenting
   the decision and its rationale.

ValKit defaults to Compliance mode and requires an explicit argument to select
Governance, so the trade-off is made deliberately rather than by omission.

## Prompt injection

The agents ValKit evaluates typically read untrusted content — a protocol, a
report, a case narrative. Content that reaches a model can carry instructions,
and the NIST Generative AI Profile treats this as a principal risk of the
technology.

ValKit addresses it in two places. The standing risk library includes prompt
injection with high severity and low detectability, linked to the out-of-scope
requirements it threatens. The adversarial dataset exercises it: the example
red-team set includes instructions embedded in content presented as protocol
text, out-of-scope requests, attempts to elicit protected identifiers, and bait
for fabricated citations.

What ValKit does **not** do is defend your agent. Input isolation, tool
allow-listing and output validation are properties of the agent under
validation. ValKit tests whether they work; it does not supply them.

## Credentials

No electronic-signature component value is ever stored, logged, returned or
included in an exception message. Verification returns only the *names* of the
components that were satisfied. Passwords are held as PBKDF2-HMAC-SHA256
verifiers with a per-user salt and compared in constant time.

The audit trail redacts secret-looking keys before writing, as a second line of
defence: the audit trail is the one store from which nothing can later be
removed, so anything that reached it would be exposed for the life of the
record.

Each entry point has its own way of getting this wrong, so each is closed
explicitly:

**The CLI** refuses a password as a command-line argument, since argv lands in
shell history and is visible in the process table. A prompt or an environment
variable are the only routes in.

**The HTTP API** takes the components in the request body and nowhere else —
never a query parameter, because query strings reach access logs, proxy logs,
browser history and referrer headers. A test walks the OpenAPI schema and
asserts no query or path parameter on any route is named like a credential, so
adding one later fails the suite rather than passing review.

The subtle one is the error path. FastAPI's default validation handler returns
the input that failed validation, which for a signing request is the password
itself, in a 422 body as durable as any log line. ValKit installs a handler that
redacts first.

**The MCP tools** never echo a credential in a tool result. A tool result is
model context and may be persisted in a transcript, so a leak there survives the
call that caused it.

**The worker** never signs at all, so it never holds a component.

A test sweeps every surface — the signature record, the rendered manifest, the
audit trail in both export forms, the generated documents, `repr()` of the
session and signature objects, every API response including the failure
responses, and the MCP tool results — and asserts no password appears in any of
them.
