# The statistical method

This document sets out how ValKit turns evaluation results into an acceptance
claim, and — more importantly — what that claim does and does not rest on. It is
written to be read by a validation lead or a statistician reviewing the
methodology, not only by an engineer.

## Why not the observed pass rate

An agent passes 176 of 180 qualification cases. The observed rate is 0.978. It
is tempting to write "accuracy 97.8%" into the report and compare it to a 95%
target.

That comparison has no confidence attached to it. The 180 cases are a sample;
the quantity of interest is the agent's behaviour on the population of cases it
will meet in use. A sample of 180 that happens to contain four failures is
consistent with a true rate meaningfully below 0.978, and the report needs to
say how far below it can reasonably be.

ValKit therefore states the **one-sided lower confidence bound**: the value the
true rate exceeds, with the stated confidence, given what was observed. For
176 of 180 at 95% confidence that is 0.9514 under the Wilson method — which is
what a signed OQ report should say, because it is the claim the evidence
supports rather than the number the sample happened to produce.

## One-sided and two-sided alpha

This is the single most common error in hand-built validation statistics, and
it always errs in the same direction: overstating confidence.

A **95% confidence interval** places 2.5% of the allowable error in each tail. A
**95% one-sided lower bound** places all 5% in the lower tail. The two produce
different numbers, and the interval's lower limit is the more conservative of
the two. Using an interval's lower limit while calling it a 95% lower bound is
merely conservative; the reverse — computing a one-sided bound and reporting it
as an interval — overstates what the data supports.

ValKit's one-sided functions take `confidence` and place `1 - confidence` in the
single tail. The identity is asserted in the test suite: a one-sided 95% lower
bound equals the lower limit of a two-sided 90% interval.

## Choosing a method

| Method | Basis | Behaviour | When |
| --- | --- | --- | --- |
| Clopper-Pearson | Exact, from the binomial tail | Guaranteed at least nominal coverage; conservative | The GxP default |
| Wilson | Score interval | Coverage close to nominal on average, occasionally below; much tighter near 0 and 1 | Where the exact method's conservatism demands an unreasonable golden set |
| Jeffreys | Bayesian, non-informative prior | Between the two | A defensible middle |
| Wald | Normal approximation | Badly behaved at extreme proportions | Never, for an acceptance claim |

**Clopper-Pearson is the default** because conservative is the right direction
to err when overstating performance has a patient-safety or data-integrity
consequence. Its cost is real: it can demand a larger qualification set for the
same claim.

**Wald is included only for comparison.** At k = n it returns exactly 1.0 —
asserting certainty from a finite sample. ValKit warns at specification load
time if it is selected.

The choice belongs in the specification and appears in the generated report, so
a reviewer can see which method produced the number rather than having to
assume.

## Sizing a qualification set

This is the part worth knowing *before* building a golden set, because
discovering it afterwards means either a wasted run or a quietly weakened
target.

With zero failures, the exact binomial bound has a closed form. The probability
of observing n consecutive passes from a process whose true rate is exactly
`target` is `target ** n`; the smallest n for which that drops below
`1 - confidence` is

```
n >= ln(1 - confidence) / ln(target)
```

At 95% confidence:

| Target | Minimum cases, zero failures |
| --- | --- |
| 0.90 | 29 |
| 0.95 | 59 |
| 0.98 | 149 |
| 0.99 | 299 |

Tolerating failures raises the requirement sharply, because each failure has to
be offset by more evidence:

| Target | 0 failures | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| 0.95 | 59 | 93 | 124 | 153 |
| 0.98 | 149 | 236 | 313 | 386 |
| 0.99 | 299 | 473 | 628 | 773 |

Two consequences follow, and they are the practical heart of this document.

**A modest golden set cannot support an aggressive target.** With 64 scored
cases, a 0.95 target under Clopper-Pearson tolerates *zero* failures. A team
that builds sixty cases and writes a 0.98 target has committed to a criterion no
result can satisfy. `valkit sample-size --target 0.98 --failures 2` answers this
in a second, and `valkit run` reports the shortfall rather than letting it
surface as a mysterious failure.

**Curating beats enlarging, up to a point.** Going from one tolerated failure to
zero at a 0.95 target saves 34 cases. Removing an ambiguous or mislabelled case
from the golden set is usually cheaper than adding thirty-four more — provided
it is removed because it was wrong, not because it failed. Removing cases
because the agent fails them is not curation, and it invalidates the whole
argument.

## What the arithmetic assumes

The binomial model assumes the qualification cases are **independent** and
**representative** of the intended use. No amount of arithmetic can establish
either. They are properties of how the set was built, and they are where a
statistical acceptance argument is actually won or lost.

*Independence.* Two cases that differ only in a name are not two observations.
Neither are twenty cases generated from one template. `valkit run` reports
duplicate inputs, and the credibility report prints the count, because
near-duplicates inflate the apparent evidence base while adding nothing.

*Representativeness.* A bound computed over cases drawn only from one form type,
one document format or one therapeutic area is a precise statement about that
subpopulation and says nothing about the rest. ValKit reports the stratum
composition and supports per-stratum breakdowns, because a metric that passes
overall while failing on one stratum is a finding that an aggregate hides.

These assumptions are stated in the generated credibility report rather than
left implicit, so a reviewer can judge them.

## Metrics that are not proportions

*Continuous scores* (a quality rating, a similarity) use a one-sided Student-t
lower bound on the mean. This assumes approximate normality of the sample mean,
which the central limit theorem makes reasonable at the sample sizes involved,
and it requires at least two observations: a single observation carries no
information about spread.

*Counts* — "no more than zero fabricated citations" — are compared directly
against a maximum. No interval is computed, because the criterion is about the
observed count and not about an underlying rate. Where the underlying rate is
what matters, a proportion metric is the right shape.

## Non-inferiority

For an assistive agent the interesting question is usually not "is it perfect"
but "is it at least as good as the process it supports". Non-inferiority holds
when the one-sided lower bound on the agent's rate exceeds `baseline - margin`.

The margin must be justified in the validation plan **before** the run. A margin
chosen after seeing the result is not a statistical argument; it is a
rationalisation, and it will be read as one.

## Judge calibration

An LLM used to grade outputs is a measuring instrument, and an unvalidated
instrument produces no evidence. ValKit quantifies its agreement with human
assessment on a labelled subset using Cohen's kappa, which corrects observed
agreement for the agreement expected by chance:

```
kappa = (p_observed - p_expected) / (1 - p_expected)
```

Sign-off is blocked below a threshold the customer sets, defaulting to 0.80.

**Kappa has two well-documented pathologies**, and reporting it alone risks both
false alarm and false comfort, so ValKit reports both indices alongside it.

*The prevalence problem.* When one label dominates — and on a well-built golden
set most cases pass, so it does — chance agreement is high and kappa is
pessimistic. Two raters can agree on 96% of cases and score a kappa of 0.78.
That is not evidence of an unreliable judge; it is what kappa does under skew.

*The bias problem.* When the two raters' marginal distributions differ
systematically, kappa falls independently of accuracy.

ValKit computes the prevalence index `(tp - tn) / n` and the bias index
`(fp - fn) / n`, and the credibility report prints them with the kappa. It also
reports the confusion counts, because the direction of error matters: a **false
pass**, where the judge accepts a case the human rejected, is the consequential
one, since it admits a defect into a signed report.

Two rules are enforced rather than left to the caller:

- **Too few labelled cases is a calibration failure, not a pass.** A judge
  compared against eight cases has not been calibrated. Defaulting to "passed"
  on insufficient evidence is the failure mode the whole product exists to
  prevent.
- **An unreadable verdict is an error, not a pass.** If the judge's response
  cannot be parsed, the sample leaves the denominator where it is visible,
  rather than being counted as acceptable.

## Errors and the denominator

A sample that fails to execute — a provider timeout, a malformed response — is
neither a pass nor a failure. ValKit excludes it from the denominator and
reports it separately.

The alternatives are both wrong. Counting an execution error as an agent failure
understates the agent and attributes an infrastructure problem to it. Dropping
it silently overstates the evidence base, because the report then claims n cases
were evaluated when fewer were. Both numbers appear in the OQ report.

Where errors exceed a configurable fraction of the run, the run is marked
**failed** rather than producing an acceptance verdict at all: a battery riddled
with provider errors is a broken apparatus, not a failing agent, and reporting
it as an acceptance failure would be a false statement about the agent.

## Statistical process control

Monitoring uses an individuals control chart, with sigma estimated from the
**moving range** (mean moving range divided by 1.128) rather than the sample
standard deviation. The reason is the same one that motivates individuals charts
generally: a process that has already shifted inflates its own standard
deviation, which widens the limits and conceals the shift. The moving range
estimates short-term variation only, so a sustained shift appears as points
outside the limits rather than as wider limits.

The point being tested is **excluded** from the limits it is tested against.
Including it lets a marginal outlier pull the limits toward itself and escape
detection — demonstrated in the test suite with a value that is caught when
excluded and hidden when included.

An individuals chart on a proportion assumes roughly constant n, which scheduled
re-evaluation against a fixed golden set usually satisfies. Where n varies
materially, `p_chart_limits` computes per-point limits from the binomial
variance and should be preferred; the docstring says so.

## Implementation and verification

The numerical core is pure Python: Lanczos log-gamma, the incomplete beta
function by modified Lentz continued fraction, its inverse by bisection, and the
normal and Student-t quantiles. No SciPy.

That is a validation decision rather than an engineering preference. In a GxP
tool every third-party package is a supplier that has to be assessed, and the
numerical core of an acceptance claim ought to be small enough for a reviewer to
read end to end and satisfy themselves it is right.

Correctness is established by comparison against an independent implementation:
reference values computed with `mpmath` at 40 decimal digits, asserted in the
test suite. `mpmath` is a test-only cross-check and is never imported at
runtime. Measured accuracy: incomplete beta to 1e-13, inverse to 1e-12,
Student-t quantiles to 1e-12.

The published anchors are asserted exactly:

- Wilson interval for 30 of 100 at 95%: [0.219, 0.396]
- Clopper-Pearson for the same: [0.2124, 0.3998]
- Zero-failure sizing at 95% confidence: 59, 149, 299 for targets 0.95, 0.98, 0.99
- Allowing one and two failures at a 0.95 target: 93 and 124

and each sizing result is checked to be the *smallest* n that works, by
confirming that n − 1 does not.
