/* Formatting, and the enum→prose lookup.
 *
 * One rule runs through all of it: the console never invents a number and never
 * dresses one up. Proportions are shown to four decimal places exactly as the
 * record states them, never as a percentage — a percentage invites a reader to
 * compare an observed rate against a target, and the comparison that matters is
 * the lower bound against the target.
 */

const LABELS = {
  ValidationStatus: {
    draft: 'Draft',
    in_validation: 'In validation',
    validated: 'Validated',
    monitoring_review: 'Validated, under monitoring review',
    invalidated: 'Invalidated',
    retired: 'Retired',
  },
  DocumentStatus: {
    draft: 'Draft',
    in_review: 'In review',
    approved: 'Approved',
    rejected: 'Rejected',
    superseded: 'Superseded',
  },
  RunStatus: {
    pending: 'Pending',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    aborted: 'Aborted',
  },
  SignatureMeaning: {
    authored: 'Authored',
    reviewed: 'Reviewed',
    approved: 'Approved',
    executed: 'Executed',
    verified: 'Verified',
    rejected: 'Rejected',
  },
  BoundMethod: {
    clopper_pearson_lower: 'Clopper-Pearson',
    wilson_lower: 'Wilson',
    jeffreys_lower: 'Jeffreys',
    wald_lower: 'Wald',
    agresti_coull_lower: 'Agresti-Coull',
  },
  RequirementKind: { user: 'User', functional: 'Functional', regulatory: 'Regulatory' },
  RtmVerdict: {
    verified: 'Verified',
    'not verified': 'Not verified',
    'not executed': 'Not executed',
    'no test': 'No test',
  },
  ChangeControlStatus: {
    open: 'Open',
    impact_assessed: 'Impact assessed',
    eval_in_progress: 'Evaluation in progress',
    eval_complete: 'Evaluation complete',
    approved: 'Approved',
    rejected: 'Rejected',
    closed: 'Closed',
  },
  AlertSeverity: { info: 'Info', warning: 'Warning', critical: 'Critical' },
  ChangeTrigger: {
    model_version: 'Model version',
    prompt_change: 'Prompt change',
    dataset_change: 'Dataset change',
    spec_change: 'Specification change',
    drift: 'Drift',
    periodic_review: 'Periodic review',
    defect: 'Defect',
    other: 'Other',
  },
};

/** Prose for an enum value, or the raw token when this console has no label.
 *
 * An unknown value is shown as the record's own value rather than guessed at or
 * upper-cased: the record is the authority on what it says.
 */
export function label(kind, value) {
  const table = LABELS[kind] || {};
  if (Object.prototype.hasOwnProperty.call(table, value)) {
    return { text: table[value], known: true };
  }
  return { text: String(value), known: false };
}

/** Four decimal places. Never a percentage. */
export function proportion(value) {
  return Number(value).toFixed(4);
}

/** A signed margin, for the one number this console computes itself. */
export function margin(value) {
  const fixed = Math.abs(value).toFixed(4);
  return (value >= 0 ? '+' : '−') + fixed;
}

export function integer(value) {
  return Number(value).toLocaleString('en-GB');
}

export function bytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const HEX64 = /^[0-9a-f]{64}$/;
export const isDigest = (value) => typeof value === 'string' && HEX64.test(value);

/** Shortest prefix length at which every digest on a screen stays distinct.
 *
 * A mechanical rule, not a claim about how many collisions exist: collect the
 * digests, and if two share their first 12 characters render both at 24.
 */
export function digestWidth(digests) {
  const seen = new Set();
  for (const d of digests) {
    const head = d.slice(0, 12);
    if (seen.has(head)) return 24;
    seen.add(head);
  }
  return 12;
}

export function timestamp(iso) {
  return typeof iso === 'string' ? iso : '';
}

export function plural(n, one, many) {
  return n === 1 ? one : many;
}

export default { label, proportion, margin, integer, bytes, isDigest, digestWidth, timestamp, plural };
