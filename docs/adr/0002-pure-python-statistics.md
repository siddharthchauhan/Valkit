# 2. Implement the statistics in pure Python

**Status:** accepted

## Context

ValKit computes confidence bounds from the incomplete beta function and the
normal and Student-t quantiles. SciPy provides all of these, tested by many more
people than will ever read this code.

But the numbers these functions produce are the substance of an acceptance
claim in a signed validation document. Two things follow. In a GxP context every
third-party package is a supplier requiring assessment, and SciPy's surface —
transitive dependencies, compiled extensions, a release cadence measured in
weeks — makes that assessment substantial. And a reviewer asked to satisfy
themselves that a bound is correct has, with SciPy, no practical way to do so
short of trusting the library.

## Decision

Implement the numerical core in pure Python from published algorithms: Lanczos
log-gamma, the incomplete beta function by modified Lentz continued fraction,
its inverse by bisection, Acklam's inverse normal CDF with a Halley refinement,
and the Student-t quantile via the incomplete beta inverse. Roughly three
hundred lines, each function documented with its algorithm and its measured
accuracy.

Establish correctness by comparison against an independent implementation:
`mpmath` at 40 decimal digits, asserted in the test suite, imported only there.

## Consequences

**For:** the numerical core can be read end to end by a reviewer and qualified
by inspection. The runtime dependency surface is PyYAML and Jinja2 and nothing
else, which makes the supplier assessment a page rather than a project.
Installation is trivial everywhere, including in a locked-down environment where
compiling SciPy is not an option.

**Against:** this is code we now own and must maintain, in a domain where subtle
errors are easy and consequential. It is slower than SciPy by a wide margin,
which does not matter at the call volumes involved — a handful of evaluations
per run, never in a loop — but would matter if that changed. And the accuracy
claims rest on our own test suite rather than on a library with a large user
base finding the edge cases for us. The mitigation is the independent
cross-check and the anchors against published values; it is a mitigation, not an
elimination.
