# 3. Hash-chain the audit trail rather than trusting the database

**Status:** accepted

## Context

21 CFR 11.10(e) requires a secure, computer-generated, time-stamped audit trail
that does not obscure previously recorded information. The obvious
implementation is an append-only table with permissions denying UPDATE and
DELETE.

That satisfies the letter of the requirement while leaving an awkward question:
what does it prove to an auditor? A permission can be granted; a trigger can be
dropped; a database file can be replaced. The control depends entirely on the
integrity of the system enforcing it, which is precisely what an auditor is
there to be sceptical about.

## Decision

Chain the records. Each row stores the SHA-256 of a canonical serialisation of
its own content **including the previous row's digest**. Verification re-derives
the whole chain and reports the first sequence number at which it breaks.

Keep the database triggers as well, and describe them accurately: they are a
guard rail against accident and a control that can be demonstrated failing in
front of an inspector, not a security boundary.

Document the digest schema in the module docstring in enough detail that a third
party can reimplement verification without reading the code.

## Consequences

**For:** integrity is verifiable arithmetically rather than on the operator's
assurance. Altering one payload, deleting one row or re-ordering two entries
invalidates every digest that follows, and detection does not require the
database to cooperate. Publishing the final digest — in a report, a
countersigned document, a separate store — lets a third party verify the trail
without trusting the system at all.

**Against:** the chain proves *internal* consistency only. An attacker who
rewrites the entire trail from the point of alteration onward produces a
self-consistent chain, and only an externally recorded digest detects that. The
docstring says so rather than implying more than is true. There is also a
practical cost: records must be appended strictly in sequence, so writes are
serialised through an IMMEDIATE transaction, which caps throughput. At the
volumes a validation produces this is irrelevant, and the tests confirm four
concurrent writers produce a gap-free chain.
