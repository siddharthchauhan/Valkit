# Compliance mapping

What each regulation or standard requires, which part of ValKit addresses it,
and what remains the customer's. Read the two sections at the end first if you
are deciding whether to rely on this tool: they set out what it does not do.

## Status matters, and is stated

The single most important distinction in this table is between what is **final**
and what is **draft**. A tool that lets the two blur will lead a customer to
claim compliance with a rule that does not yet exist, which is worse than
claiming nothing. ValKit maintains the distinction in this document, in the
generated credibility report, and in the README.

| Instrument | Status |
| --- | --- |
| 21 CFR Part 11 | **Final.** Rule issued 1997, in force |
| GAMP 5 2nd edition, incl. Appendix D11 (AI/ML) | **Published**, 2022 |
| ISPE GAMP AI Guide | **Published**, July 2025 |
| ICH Q9(R1) Quality Risk Management | **In force** |
| ICH E6(R3) Good Clinical Practice | **In force**; principles and Annex 1 applied in the EU from 23 July 2025 |
| EMA reflection paper on AI in the medicinal product lifecycle | **Final**, September 2024 |
| FDA Computer Software Assurance guidance | **Final**, September 2025 — but see the scope note below |
| ISO/IEC 42001:2023 (AI management systems) | **Published**, certifiable |
| NIST AI RMF 1.0 and the Generative AI Profile | **Published**, voluntary |
| EU AI Act (Regulation 2024/1689) | **In force**; obligations phase in |
| **FDA draft guidance on AI to support regulatory decision-making** | **DRAFT**, January 2025. **Not final.** |
| **EU Annex 11 revision and new Annex 22 (AI)** | **DRAFT**, July 2025. **Not final.** |

Two scope notes that are easy to get wrong:

**The FDA CSA guidance is final but narrowly scoped.** It addresses computer
software used in medical device production and the quality system. Applying its
reasoning to a clinical or GCP-context LLM agent is an analogy — a good one, and
one many organisations will draw — but it is not a direct mandate, and a
validation package should not present it as one.

**The FDA AI guidance is a draft.** Its seven-step credibility framework is a
sound way to structure the argument, and ValKit generates documents in that
shape. But comments closed in April 2025 and the final text may differ. ValKit
applies it as best practice and says so on the face of the generated report.

## Regulation by regulation

### 21 CFR Part 11 — Electronic Records; Electronic Signatures

| Clause | Requirement | ValKit | Yours |
| --- | --- | --- | --- |
| 11.10(a) | Validation of systems to ensure accuracy, reliability and consistent intended performance | The whole product, applied to the agent; `docs/validating-valkit.md` for ValKit itself | Deciding the validation is adequate for your use |
| 11.10(b) | Ability to generate accurate and complete copies in human-readable and electronic form | `AuditTrail.export_text` and `export_jsonl`; documents in Markdown and HTML | Retention and retrieval procedures |
| 11.10(c) | Protection of records throughout the retention period | `valkit.vault` — content-addressed, write-once, retention enforced | Setting the retention period; backup and disaster recovery |
| 11.10(d) | Limiting access to authorised individuals | `valkit.esign.identity` | Binding it to your directory; account governance |
| 11.10(e) | Secure, computer-generated, time-stamped audit trail that does not obscure previously recorded information | `valkit.audit` — hash-chained, append-only, independently verifiable | Reviewing the trail; retention |
| 11.10(k) | Control over systems documentation, including revision and change control | `valkit.change` | Your document control system |
| 11.50(a) | Signed records show printed name, date and time, and meaning of the signature | `Signature`; `SignatureService.manifest` | Ensuring printed names are the individual's actual name |
| 11.50(b) | Those items appear in any human-readable form | The signature block is rendered into the document | Reviewing the rendered form |
| 11.70 | Signatures linked to their records so they cannot be excised, copied or transferred | The signature binds the SHA-256 of the exact content signed; verification checks both the digest and the document identifier | — |
| 11.100(a) | Each signature unique to one individual, never reused or reassigned | Enforced by the identity store | Governance of joiners and leavers |
| 11.200(a)(1)(i) | First signing of a continuous session uses all components; subsequent use at least one component only executable by the individual | `SigningSession`; a user id alone is refused for subsequent signings | Defining what constitutes controlled system access in your environment |
| 11.200(a)(1)(ii) | Signings outside a continuous session use all components | Session expiry is treated as the regulatory boundary it is | — |
| 11.200(a)(2) | Signatures used only by their genuine owners | Sessions are not shareable | Training; the administrative controls 11.200(a)(3) requires |
| 11.300(b) | Identification codes and passwords periodically checked, recalled or revised | Password ageing blocks signing | Your credential policy |

**Not addressed by ValKit and squarely yours:** 11.10(i) training of personnel,
11.10(j) the written policy holding individuals accountable for their electronic
signatures, 11.100(b) and (c) identity verification and the certification to
FDA, and 11.300(d) transaction safeguards against unauthorised use. These are
organisational controls a tool cannot supply.

### FDA draft guidance on AI to support regulatory decision-making (January 2025)

**Draft. Not final.** The seven-step credibility framework:

| Step | ValKit |
| --- | --- |
| 1. Question of interest | `context_of_use.question_of_interest`; opens the credibility report |
| 2. Context of use | `context_of_use.role`, in-scope and out-of-scope; out-of-scope entries become testable negative requirements |
| 3. Model risk | The influence-by-consequence matrix in `valkit.spec.risk`, as an explicit table with escalation rules that only ever raise |
| 4. Credibility assessment plan | Acceptance criteria, the qualification set and its composition, judge calibration thresholds — all fixed before execution |
| 5. Execution | The run record: model, seed, dataset digest, harness configuration digest |
| 6. Results and deviations | Metric results with bounds; deviations with dispositions |
| 7. Adequacy for the context of use | An explicit conclusion, with limitations carried forward from the specification |

**Yours:** determining the question of interest and the context of use
accurately. ValKit applies rules to what you declare; a context of use that
understates how the output is really used produces a risk assessment that
understates the risk, and nothing in the tool can detect that.

### GAMP 5 2nd edition, and Appendix D11

| Requirement | ValKit | Yours |
| --- | --- | --- |
| Risk-based, proportionate lifecycle | The risk assessment drives the recommended evidence | Agreeing the proportionality is right |
| Critical thinking | — | **Entirely yours.** Generating documents is not critical thinking, and a generated risk assessment is a starting point for review |
| Requirements traceability | `valkit.trace`; the RTM reports gaps as prominently as coverage | Reviewing whether the requirements are the right ones |
| Supplier assessment | `docs/validating-valkit.md`; the dependency inventory | Assessing ValKit and your model provider as suppliers |
| Category-appropriate rigour | Category recorded and used in the risk determination | Confirming the categorisation |
| D11: AI/ML lifecycle, training data, performance metrics, monitoring | Dataset provenance and digests; metric definitions; `valkit.drift` | Data governance; whether the qualification data represents production |

### ICH Q9(R1) — Quality Risk Management

Severity, probability and detectability are combined in the two-step method the
guideline describes: severity and probability give a priority, which
detectability then modifies. Residual risk after mitigation is never recorded as
eliminated — a control reduces risk by at most one class, and claiming a control
removes a failure mode is a statement that does not survive inspection.

**Yours:** the risk policy, the acceptance criteria for residual risk, and the
decision that a mitigation is effective.

### ICH E6(R3) — Good Clinical Practice

Applies where the agent touches a clinical trial process. E6(R3)'s emphasis on
quality by design and on fitness for purpose of computerised systems is served
by the validation record; the trial-level quality management, sponsor oversight
and vendor qualification remain the sponsor's.

### EU Annex 11 revision and draft Annex 22 (AI)

**Draft. Not final.** Published for consultation in July 2025; the final text
may differ materially. The direction of travel — model-specific evidence,
expanded data-integrity and electronic-signature expectations — is consistent
with what ValKit produces, and a package built now should transfer. But no
claim of Annex 22 compliance can be made against a draft, and ValKit does not
make one.

### EMA reflection paper on AI in the medicinal product lifecycle (final, September 2024)

Risk-based and human-centred use across the lifecycle. The human-in-the-loop
declaration is treated as a control the risk assessment depends on, which is why
it becomes a requirement verified in performance qualification rather than an
assumption.

### EU AI Act (Regulation 2024/1689)

In force, with obligations phasing in. **Whether a given agent is high-risk
under Annex III is a legal classification, and neither ValKit nor its authors
make it.** Where an agent is in scope, the material ValKit produces —
documentation, logging, records of human oversight, performance evidence — maps
onto several technical-documentation obligations, but conformity assessment and
the classification itself are yours.

### ISO/IEC 42001:2023 and NIST AI RMF

ValKit produces artefacts an AI management system consumes: risk assessments,
performance evidence, monitoring records, change control. It does not operate
the management system, and certification is not something a tool confers.

For the NIST Generative AI Profile, the adversarial dataset and the
prompt-injection risk in the standing risk library address part of the *Measure*
and *Manage* functions. *Govern* is organisational and out of scope.

## What ValKit does not do

Stated plainly, because a tool that implies otherwise is a liability:

- **It does not make anyone compliant.** It produces evidence and documents.
  Compliance is a property of an organisation and its quality system.
- **It does not determine your context of use or your risk.** It applies
  documented rules to what you declare. A wrong declaration produces a
  confidently wrong assessment.
- **It does not curate your qualification set**, which is where the statistical
  argument is actually won or lost.
- **It does not replace critical thinking**, review, or subject-matter
  judgement. A generated document is a draft for a qualified person to review
  and take responsibility for.
- **It does not validate itself into your environment.** See
  `docs/validating-valkit.md` for the qualification package and what remains
  yours.
- **It does not cover the organisational controls** Part 11 requires alongside
  the technical ones: training, accountability policies, identity verification,
  and the FDA certification under 11.100(c).

## What is yours

- The context-of-use and risk determinations, and whether they are honest.
- Curating a qualification set that is representative and independent.
- Deciding the acceptance criteria are appropriate to the decision being made.
- Reviewing every generated document on its merits before signing it.
- The quality system: procedures, training, account governance, periodic review,
  and the predicate-rule compliance that Part 11 sits on top of.
- Determining which regulations apply to your use, and taking legal advice on
  the ones that do.

## A note on this document

It reflects the regulatory position as understood at the time of writing and is
not legal or regulatory advice. Statuses change; the draft instruments above in
particular are expected to. Verify current status before relying on any entry
here.
