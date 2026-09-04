/* Every static string the console shows, in one file.
 *
 * Here rather than inlined so that the whole of what this interface asserts can
 * be reviewed in one sitting — which is what a quality function will actually
 * ask to do — and so that a regulatory claim cannot be quietly introduced in a
 * template somewhere.
 *
 * Server-authored prose never passes through this file. Blockers, conditions,
 * satisfied statements, metric rationales, requirement text, findings and error
 * bodies are rendered verbatim from the API, because they are also written into
 * the audit trail: a paraphrase here would make the screen and the trail
 * disagree.
 */

export const COPY = {
  // -- the standing disclaimer -------------------------------------------
  DISC_1:
    'ValKit does not make anyone compliant. It produces evidence and documents. ' +
    'It does not determine your context of use, curate your qualification set, ' +
    'replace the critical thinking GAMP 5 requires, or substitute for a quality system.',
  DISC_REST: [
    'Records here are append-only. There is no PUT, PATCH or DELETE: a document cannot ' +
      'be edited, a signature cannot be withdrawn, an audit record cannot be amended. To ' +
      'change a document, generate a new one that supersedes it; the superseded document ' +
      'and its signatures remain part of the record.',
    'Acting as is attribution, not authentication. Nothing in ValKit verifies the claim. ' +
      'Signing is the exception: a signature is verified against the identity store’s ' +
      'components regardless of the header.',
    'Validations are held in this instance’s memory and do not survive a restart. The ' +
      'audit trail and the evidence vault are durable and are unaffected.',
    'Verification proves internal consistency. It detects any change made after the fact ' +
      'by anyone who does not rewrite the entire remainder of the trail. It does not prove ' +
      'that a wholesale rewrite never happened; publishing the chain digest externally is ' +
      'what does that.',
    'A generated document is a draft for a qualified person to review and take ' +
      'responsibility for.',
  ],

  // -- identity ----------------------------------------------------------
  ID_UNSET:
    'Required before you create a record or approve a document. This identifies the actor in ' +
    'the audit trail; it is not authentication.',
  ID_SET: (at) =>
    `Actions in this tab are attributed to this identity from ${at} (UTC).`,

  // -- integrity ---------------------------------------------------------
  INT_CHAIN: (n) =>
    `Audit chain — intact. ${n} ${n === 1 ? 'record' : 'records'} re-derived from the genesis record.`,
  INT_VAULT: (n) =>
    `Evidence vault — ${n} of ${n} ${n === 1 ? 'object' : 'objects'} verified against ` +
    `${n === 1 ? 'its digest' : 'their digests'}.`,
  INT_UNRUN: 'Not yet verified. Nothing below has been checked.',
  INT_FAILED_REQUEST:
    'Integrity could not be established: the verification request did not complete. ' +
    'Nothing below has been checked.',
  INT_LIMIT:
    'Verification proves internal consistency: it detects any change made after the fact by ' +
    'anyone who does not rewrite the entire remainder of the trail. It does not prove that a ' +
    'wholesale rewrite never happened; publishing the chain digest externally is what does that.',
  INT_UNAVAILABLE: (a, b) => `This instance reports itself unavailable. ${a} ${b}`.trim(),

  // -- interdiction ------------------------------------------------------
  INTERDICT_H: 'Recorded evidence failed verification',
  INTERDICT_1:
    'This is an integrity failure, not an acceptance failure. It does not mean the agent ' +
    'missed a target; it means the record of what happened cannot be trusted until it is ' +
    'investigated.',
  INTERDICT_2:
    'Nothing else in this console should be relied on while this holds. Signing is refused, ' +
    'and no screen here can repair it: the hash chain and the content addressing are the ' +
    'controls, and a failure means something bypassed them.',
  INTERDICT_3:
    'Export the audit trail and preserve it before anything else. Then follow your ' +
    'deviation procedure — this is a data-integrity event, and the tool is not the ' +
    'right place to decide what happens next.',

  // -- record index ------------------------------------------------------
  IDX_H: 'Record index',
  IDX_LEDE:
    'What this instance holds, and whether it can be trusted. Every screen here is reachable ' +
    'by its address with no identity set, and no screen reachable without one writes anything.',
  IDX_TYPEFACE:
    'Two typefaces, two authors. Serif is prose the API wrote and is rendered verbatim; ' +
    'this sans-serif is what the console wrote. Where the console has something to add ' +
    'beside a server sentence it is marked Console note.',
  IDX_VAL_H: 'Validations in memory',
  IDX_VAL_EMPTY:
    'None. This instance holds no validation. Validations do not survive a restart; the ' +
    'audit trail and the evidence vault do, and are listed in the navigation above.',
  IDX_SPEC_H: 'Specifications ingested',
  IDX_SPEC_EMPTY: 'None. Nothing has been ingested in this instance.',

  // -- the gate ----------------------------------------------------------
  GATE_HOLD: 'In validation',
  GATE_READY: 'Validated',
  GATE_SUMMARY_HOLD: (b, s, c) =>
    `Not yet validated. ${b} condition${b === 1 ? '' : 's'} of the validated gate ` +
    `do${b === 1 ? 'es' : ''} not hold. ${s} hold. ` +
    `${c} obligation${c === 1 ? '' : 's'} remain${c === 1 ? 's' : ''} outstanding.`,
  GATE_SUMMARY_READY: (s) =>
    `Every condition of the validated gate holds. All ${s} are listed below.`,
  GATE_CONJUNCTIVE:
    'The gate is conjunctive: every condition must hold. Each is evaluated independently, so ' +
    'what follows is everything outstanding, not the first thing. The gate reports the ' +
    'conditions it evaluated on this request and does not publish a fixed total, so no ' +
    'proportion is shown.',
  GATE_READY_CONDITIONS: (c) =>
    `${c} obligation${c === 1 ? '' : 's'} remain${c === 1 ? 's' : ''} outstanding. ` +
    'Validated status is conditional on completing them.',
  GATE_READY_LIMIT:
    'This states that the evidence ValKit holds satisfies every condition of the gate. It is ' +
    'not a statement that your organisation is compliant.',
  GATE_LIFECYCLE_NULL: (status) =>
    `Lifecycle status on the record: ${status}. The record carries no validation date.`,
  GATE_LIFECYCLE_SET: (status, at) =>
    `Lifecycle status on the record: ${status}, validated ${at} (UTC).`,

  BLOCKERS_H: 'Not yet validated because',
  BLOCKERS_INTRO:
    'Each of these is a condition of the validated gate that does not hold. Any one of them ' +
    'is enough to withhold validated status.',
  CONDITIONS_H: 'Outstanding conditions',
  CONDITIONS_INTRO:
    'These do not block the qualification evidence. Validated status depends on completing ' +
    'them, and they are carried into the validation summary report.',
  SATISFIED_H: 'Satisfied',
  SATISFIED_INTRO:
    'Each of these is a condition of the validated gate that holds. This is the evidence a ' +
    'signature would rely on.',
  NONE: 'None.',

  GATE_COVERAGE_NOTE: (n, ids) =>
    `Console note. Coverage is complete: every critical requirement has a verifying test. ` +
    `${n} of those tests ${n === 1 ? 'has' : 'have'} not been executed — ${ids}. ` +
    'Covered means a test is linked. Verified means it ran.',
  GATE_INTEGRITY_BLOCKER:
    'The evidence cannot be trusted until this is investigated. Nothing else on this page ' +
    'should be relied on.',
  GATE_SPEC_NOTE:
    'The specification was ingested before the run. The audit trail records it.',
  GATE_SKIPPED_EMPTY: 'None. Every document type in the package order was generated.',
  GATE_WARNINGS_EMPTY: 'None.',
  GATE_404: (id) =>
    `No validation with identifier ${id} in this instance. Validations are held in memory ` +
    'and do not survive a restart. The audit trail and the evidence vault are durable.',
  GATE_NO_RUN: 'No evaluation run is attached to this validation.',
  NO_REMEDY: 'No remedy is available from this console.',
  NO_REMEDY_DEPLOYMENT:
    'No remedy is available from this console; this is a deployment configuration.',

  // -- acceptance --------------------------------------------------------
  ACC_H: 'Acceptance',
  ACC_LEDE:
    'The lower bound is the claim. The observed rate describes this sample; the bound is the ' +
    'value the true rate exceeds with the stated confidence, and it is what the signed report ' +
    'asserts. Every number below except the margin came from the run record.',
  ACC_MARGIN_NOTE: 'The only number on this screen the console computed.',
  ACC_ONE_SIDED:
    'One-sided. There is no upper limit and no interval: the question is whether the true ' +
    'rate is above the target, so only the floor is stated.',
  ACC_CAL_H: 'Judge calibration',
  ACC_CAL_INTRO:
    'An LLM judge is an unvalidated measuring instrument until its agreement with human ' +
    'labels is quantified. Cohen’s kappa corrects for the agreement expected by chance; ' +
    'raw agreement does not, and the two are reported together because either alone misleads.',
  ACC_CAL_EMPTY: 'None. No judge calibration is recorded on this run.',
  ACC_WARN_H: 'Warnings recorded when the run was assembled',
  ACC_DENOM: (n, errors) =>
    errors
      ? `${n} scored, ${errors} excluded as ${errors === 1 ? 'an execution error' : 'execution errors'}`
      : `${n} scored, none excluded`,
  ACC_EMPTY: 'None. No acceptance criteria were evaluated on this run.',

  // -- chain -------------------------------------------------------------
  CHAIN_H: 'Traceability',
  CHAIN_LEDE:
    'The chain of reasoning: what the user requires, what could go wrong, which test verifies ' +
    'it, whether that test ran, and what evidence it left. A package where this chain is ' +
    'unbroken can be audited; one where it is not cannot, however good the documents look.',
  CHAIN_COVERED_VS_VERIFIED:
    'Covered means a test is linked to the requirement. Verified means that test ran. They ' +
    'are not synonyms, and this record contains requirements that are covered but not verified.',
  CHAIN_FINDINGS_H: 'Findings',
  CHAIN_FINDINGS_EMPTY: 'None. The traceability chain has no findings.',
  CHAIN_EMPTY: 'None. This validation has no traceability matrix.',

  // -- package -----------------------------------------------------------
  PKG_H: 'Package',
  PKG_LEDE:
    'The documents generated from this record, and their approvals. A document is a draft ' +
    'until the approvals its specification requires are applied; nothing here can be edited.',
  PKG_EMPTY: 'None. No documents have been generated for this validation.',
  PKG_VOID: (reason) =>
    `Console note. This document carries a signature that does not verify: ${reason} ` +
    'It is excluded from the signing queue and its approvals cannot be treated as met.',

  // -- signing -----------------------------------------------------------
  SIGN_H: 'Signing queue',
  SIGN_LEDE:
    'A signature under 21 CFR Part 11 subpart C. What you apply is an assertion about a ' +
    'specific document, bound to the digest of the exact bytes shown on its record screen.',
  SIGN_NO_IDENTITY:
    'Set who you are acting as in the masthead before signing. The identification code the ' +
    'signature is claimed for is that value.',
  SIGN_READ_GATE:
    'A document can be selected only after it has been opened in this browser tab. This is a ' +
    'local aid to you, not a record: it is never sent to the API and never printed, because ' +
    'the console cannot observe that you read anything and will not assert that it did.',
  SIGN_COMPONENTS:
    'All signature components are sent with every signing. No signing session exists over ' +
    'this API, so every signing here is a signing outside a continuous session, which is ' +
    'what 21 CFR 11.200(a)(1)(ii) requires.',
  SIGN_CREDENTIAL_NOTE:
    'The password is read once when you press Apply, sent in the request body, and cleared ' +
    'in the same tick. It is never placed in a URL, a header, browser storage, or any log ' +
    'line. sign.js is the only file in this console that reads it.',
  SIGN_PROBE:
    'The first document is attempted alone. If you are not among the approvers the ' +
    'specification names, learning that costs one refusal rather than fifteen.',
  SIGN_NOT_ATTEMPTED: 'Not attempted.',
  SIGN_NO_RETRY:
    'There is no retry buffer. Re-attempting the remaining documents requires typing the ' +
    'credential again, because nothing here held it.',
  SIGN_APPLIED: (n, total) => `${n} of ${total} applied.`,
  SIGN_APPLIED_EMPTY: 'None yet. Signatures applied in this session are listed here as they land.',
  SIGN_REJECT_WARNING:
    'A rejected signature sets this document’s status to rejected and permanently ' +
    'prevents its approvals from being met. There is no PUT, PATCH or DELETE in this API: it ' +
    'cannot be undone. To change the document, generate a new one that supersedes it. Type ' +
    'REJECT to confirm.',
  SIGN_PRINTED_NAME_NOTE:
    'The printed name is a regulated field: 21 CFR 11.50(a)(1) requires the individual’s ' +
    'actual name, which is not the identification code. A manifest reading the code in the ' +
    'printed-name row would be a defective signed record.',
  SIGN_QUEUE_EMPTY: 'None. Every document already carries the approvals its specification requires.',

  // -- documents ---------------------------------------------------------
  DOC_LEDE:
    'The record as generated. Markdown is the bytes the digest covers; the rendering is the ' +
    'human-readable form 21 CFR 11.50(b) asks for, and is shown in an isolated frame.',
  DOC_DIGEST_NOTE:
    'Console note. This console never computes a digest. Every digest shown came from an ' +
    'API field.',
  DOC_UNSIGNED_NOTE:
    'Console note. The generated Markdown carries an Approvals section written when the ' +
    'document was drafted, which still reads as unsigned. The signature manifest is appended ' +
    'below it rather than replacing it, so the rendering contradicts itself. The approvals ' +
    'panel above is authoritative; this is a defect in the document renderer, recorded here ' +
    'rather than hidden.',
  DOC_VERIFY_NONE: 'No signatures to check.',
  DOC_404: (id) => `No document with identifier ${id} in this instance.`,

  // -- audit -------------------------------------------------------------
  AUD_H: 'Audit trail',
  AUD_LEDE:
    'The 21 CFR 11.10(e) record: secure, computer-generated, time-stamped, and hash-chained ' +
    'so that it does not obscure previously recorded information. Each record carries the ' +
    'digest of the one before it.',
  AUD_SCOPE: (shown, total, limit) =>
    `Showing the ${shown} most recent of ${total} records (limit ${limit}). The API returns ` +
    'the tail of the trail, so on a longer trail the genesis record is not reachable here; ' +
    'the text and JSONL exports carry the whole of it.',
  AUD_EMPTY: 'None. No audit record matches this filter.',

  // -- evidence ----------------------------------------------------------
  EV_H: 'Evidence vault',
  EV_LEDE:
    'Content-addressed and write-once: the identifier is the SHA-256 of the bytes, so an ' +
    'identifier that resolves is itself proof of integrity, and overwriting is impossible by ' +
    'construction rather than merely forbidden.',
  EV_EMPTY: 'None. The evidence vault holds no object matching this filter.',

  // -- digest resolver ---------------------------------------------------
  DIG_H: 'Digest',
  DIG_LEDE:
    'Where this digest appears in what this console can see. The API has no digest lookup, so ' +
    'this resolves client-side over a stated window rather than over the whole record.',
  DIG_WINDOW: (ev, aud) =>
    `Searched ${ev} evidence objects and the ${aud} most recent audit records. A digest that ` +
    'is not listed below may still exist outside that window; this is not a statement that it ' +
    'does not exist.',
  DIG_NONE: 'Not found in the window searched.',
  DIG_INVALID: 'Not a SHA-256 digest. A digest is 64 hexadecimal characters.',

  // -- monitoring --------------------------------------------------------
  MON_H: 'Monitoring',
  MON_LEDE:
    'A validated status with no monitoring decays silently: a model provider can change ' +
    'behaviour with no change on your side. Observed values are listed as recorded.',
  MON_NO_CHART:
    'No control chart is drawn here. Drawing one would mean a second, unqualified ' +
    'implementation of a regulated computation that already exists in valkit.drift.spc; the ' +
    'observed values are shown instead, and the control rules are evaluated server-side.',
  MON_EMPTY: 'None. No observations are recorded for this agent.',
  MON_CC_H: 'Change control',
  MON_CC_EMPTY: 'None. No change control is open for this agent.',

  // -- specification and run ---------------------------------------------
  SPEC_H: 'Specification and run',
  SPEC_LEDE:
    'What the agent is for, how it is used, what it must achieve and who signs it off. The ' +
    'risk assessment, the requirements and the test cases are all derived from this — ' +
    'nothing here is entered twice.',
  SPEC_NEEDS_IDENTITY:
    'This screen writes to the audit trail. Set who you are acting as in the masthead first.',
  SPEC_WARN_H: 'Warnings',
  SPEC_WARN_INTRO:
    'Constructs that are legal but weaken the resulting validation package. They do not stop ' +
    'a run.',
  SPEC_RUN_NOTE:
    'Running the battery generates the package but stops short of signing. A pipeline that ' +
    'signed on the author’s behalf would defeat the purpose of requiring a signature.',
  SPEC_ESCALATED: (derived, actual) =>
    `Console note. The declared risk class was ${derived}; the assessment raised it to ` +
    `${actual}. Escalation rules only ever raise.`,

  // -- print -------------------------------------------------------------
  PRINT_H: 'Print assembly',
  PRINT_LEDE:
    'Everything needed to read this validation on paper, in one document: the gate and its ' +
    'reasoning, the acceptance evidence, the traceability matrix, the approvals, and the ' +
    'digest register. Use the browser’s print command.',
  PRINT_REGISTER_H: 'Digest register',
  PRINT_REGISTER_INTRO:
    'Every digest referenced above, in full, so a printed copy is resolvable on paper.',

  // -- generic -----------------------------------------------------------
  ROUTE_UNKNOWN: 'No screen at that address. The record index below lists what this instance holds.',
  PENDING: (noun) => `Reading ${noun}.`,
  REGION_FAILED: (noun) =>
    `Console note. ${noun} is not shown because the request did not complete. Nothing on ` +
    'this screen stands in for it.',
  REQUEST_FAILED: 'The request did not complete.',
  DRAFT_GUIDANCE:
    'This follows the seven-step credibility framework in FDA’s January 2025 draft ' +
    'guidance. That guidance is a draft and has not been finalised. The framework is applied ' +
    'because it is a defensible way to structure the argument, not because compliance with it ' +
    'is currently required.',
};

export default COPY;
