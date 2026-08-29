# 4. Make offline deterministic evaluation a first-class path

**Status:** accepted

## Context

Evaluating an LLM agent means calling a model, which is non-deterministic,
costs money, needs credentials and requires a network. A test suite built on
that would be slow, flaky and unrunnable in CI.

The usual answer is to mock the provider in tests and treat the real path as the
only one that matters. For this product that answer is insufficient, because of
what the product is: a tool that generates evidence has to be able to
demonstrate that it computes the *right* acceptance decision from a *known* set
of outputs. That demonstration is only possible when the outputs are known, and
a mock buried in a test file cannot serve as qualification evidence.

## Decision

Make the deterministic fixture provider a supported part of the product rather
than test scaffolding. It resolves from a model reference like any other
provider, produces stable outputs derived from the reference answers, and
injects a declared mixture of wrong answers, refusals and provider failures —
declared by the dataset itself, so the same mixture arises wherever the run is
invoked from.

Make it produce a deliberately imperfect pass rate. A validation package
generated from a flawless run demonstrates none of the deviation handling a real
one must.

Give the fixture judge a small default disagreement rate for the same reason: a
judge that agrees with the reference on every case reports a Cohen's kappa of
1.0, which is exactly the degenerate result that tells a reader nothing.

## Consequences

**For:** the whole test suite is hermetic, fast and deterministic. The
demonstration runs on a laptop with no credentials. The tool can qualify itself
against known inputs and known expected outputs. And a customer can develop and
review a specification end to end before spending anything on model calls.

**Against:** a fixture provider cannot exercise the failure modes of a real one —
rate limits, partial responses, latency distributions, the specific ways a real
model is wrong. Those need a live run against a real provider, and the fixture
path must not be mistaken for one. Nothing in a fixture run is evidence about an
agent; it is evidence about ValKit. The distinction is recorded on every run,
because the provider identity is part of the harness record.
