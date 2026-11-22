import type {
  ApprovalState,
  BlockStatusReason,
  BucketLocation,
  DiffSummary,
  PipelineRun,
  PipelineRunBlock,
  RollbackEntry,
  RunVerdict,
  SinkDelivery,
  StopReason,
} from '../api'

// A Validate rule, as saved, in words (ADR-014 §3: the stored rule stays {kind, …, negate}).
const OPS: Record<string, [string, string]> = {
  lt: ['is below', 'is not below'],
  le: ['is at most', 'is above'],
  gt: ['is above', 'is not above'],
  ge: ['is at least', 'is below'],
  eq: ['equals', 'does not equal'],
  ne: ['does not equal', 'equals'],
}

function shown(value: unknown): string {
  return typeof value === 'string' ? `'${value}'` : JSON.stringify(value)
}

// A bound value names its input; `expected` is what it was on this run.
function operand(value: unknown, expected: unknown): string {
  if (value && typeof value === 'object' && '$input' in value) {
    const name = String((value as { $input: unknown }).$input)
    return expected === undefined ? `input ${name}` : `input ${name} (${shown(expected)})`
  }
  return shown(value)
}

export function ruleInWords(rule: Record<string, unknown>, expected?: unknown): string {
  const not = rule.negate === true
  const path = String(rule.path ?? '')
  switch (rule.kind) {
    case 'present':
      return `${path} ${not ? 'is absent' : 'is present'}`
    case 'type':
      return `${path} ${not ? 'is not' : 'is'} a ${String(rule.type)}`
    case 'contains':
      return `${not ? 'does not contain' : 'contains'} ${operand(rule.text, expected)}`
    case 'min_length':
      return not
        ? `is shorter than ${String(rule.chars)} characters`
        : `is at least ${String(rule.chars)} characters`
    case 'compare': {
      const [yes, no] = OPS[String(rule.op)] ?? [String(rule.op), `not ${String(rule.op)}`]
      return `${path} ${not ? no : yes} ${operand(rule.value, expected)}`
    }
    case 'json_schema':
      return not ? 'does not match the schema' : 'matches the schema'
    case 'changed':
      return 'the content changed'
    default:
      return JSON.stringify(rule)
  }
}

export function verdictInWords(v: RunVerdict): string {
  // A change gate (ADR-016 §5e): not met = the same content as an earlier check.
  if (v.rule.kind === 'changed') {
    return `unchanged — the same content as the ${v.matched === 'waiting' ? 'check waiting for approval' : 'last check'}`
  }
  const rule = ruleInWords(v.rule, v.expected)
  const found = v.found === null || v.found === undefined ? '' : `; found ${shown(v.found)}`
  return `rule ${v.rule_index + 1} not met: ${rule}${found}`
}

export function skipReasonInWords(reason: BlockStatusReason | null, block: string | null): string | null {
  switch (reason) {
    case 'stopped':
      return `skipped — the run stopped at ${block ?? 'an earlier step'}`
    case 'upstream_failed':
      return `skipped — ${block ?? 'an earlier step'} failed`
    case 'cancelled':
      return 'skipped — the run was cancelled'
    case 'not_started':
      return 'skipped — the run could not start'
    case 'alert_only':
      return 'skipped — alert only (sends when the run breaks)'
    case 'awaiting_approval':
      return 'waiting for approval'
    case 'not_recorded':
      return 'skipped — reason not recorded'
    default:
      return null
  }
}

// ADR-016 §10's approval endings; a Validate stop is named by its block.
const APPROVAL_STOPS: Partial<Record<StopReason, string>> = {
  approval_rejected: 'approval rejected',
  approval_expired: 'approval expired',
  approval_superseded: 'superseded by a later check',
}

export function approvalStopInWords(reason: StopReason | null | undefined): string | null {
  return (reason && APPROVAL_STOPS[reason]) ?? null
}

// The run list's label: from the run row alone, no block reads (PRD-019 D9).
export function stopLabel(run: { stopped_at_block_id: string | null; stop_reason?: StopReason | null }): string | null {
  if (!run.stopped_at_block_id) return null
  const approval = approvalStopInWords(run.stop_reason)
  return approval ? `stopped: ${approval}` : `stopped at ${run.stopped_at_block_id}`
}

const ANSWERS: Record<ApprovalState, string> = {
  not_requested: 'not asked yet',
  waiting: 'waiting for an answer',
  approved: 'approved',
  rejected: 'rejected',
  expired: 'expired — nobody answered by the deadline',
  superseded: 'superseded — a later check found other content',
  closed: 'closed without an answer',
}

export function approvalStateInWords(state: ApprovalState): string {
  return ANSWERS[state]
}

// ApprovalResponse.state from a run's block (core/approvals.approval_state).
export function approvalState(b: PipelineRunBlock): ApprovalState {
  if (b.approval_answer) return b.approval_answer
  if (b.status === 'waiting' && b.approval_deadline) return 'waiting'
  if (b.status === 'pending' || b.status === 'waiting') return 'not_requested'
  return 'closed'
}

// "15 Nov 06:00 (in 3 h)" — the deadline must be stated wherever the request shows (PRD-018 R5).
export function deadlineInWords(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  const minutes = Math.round((at.getTime() - now.getTime()) / 60_000)
  const left =
    minutes <= 0
      ? 'passed'
      : minutes < 120
        ? `in ${minutes} min`
        : minutes < 48 * 60
          ? `in ${Math.round(minutes / 60)} h`
          : `in ${Math.round(minutes / 1440)} days`
  return `${at.toLocaleString()} (${left})`
}

// What changed against the baseline, in one line per block (the full diff is on the run page).
export function changeSummaryInWords(summary: Record<string, DiffSummary> | null): string[] {
  return Object.entries(summary ?? {}).map(([block, d]) => {
    if (d.status !== 'ok') return `${block}: no diff (${d.reason ?? d.status.replace('_', ' ')})`
    if (d.kind === 'extraction') return `${block}: ${d.paths_changed ?? d.changes?.length ?? 0} values changed`
    return `${block}: +${d.lines_added ?? 0} −${d.lines_removed ?? 0} lines`
  })
}

// Old failed runs have no failed_block_id; their error starts "<block>: ".
export function failedBlock(run: PipelineRun): string | null {
  if (run.status !== 'failed') return null
  return run.failed_block_id ?? (run.error?.match(/^([^:\s]+): /)?.[1] ?? null)
}

// A stop set to delete releases every stored output and clears the result (ADR-014 §6).
export function resultDeletedOnStop(run: PipelineRun): boolean {
  return !!run.stopped_at_block_id && run.result_path === null && run.blocks.some(b => b.collected_at)
}

function isBucket(location: unknown): location is BucketLocation {
  return !!location && typeof location === 'object' && 'bucket' in location && 'key' in location
}

function host(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

// The exact place a delivery wrote (PRD-017 R3): what a user needs to remove it by hand.
export function locationInWords(d: Pick<SinkDelivery, 'kind' | 'location'> | RollbackEntry): string {
  if (isBucket(d.location)) {
    const region = d.location.region ? ` (${d.location.region})` : ''
    return `bucket ${d.location.bucket}, key ${d.location.key} at ${host(d.location.endpoint)}${region}`
  }
  return `${d.kind} ${JSON.stringify(d.location)}`
}

// PRD-017 R3's sink states, from (status, undo_status) as ADR-014 §5c maps them.
export const UNDO_PILL: Record<string, string> = {
  undoing: 'undoing',
  undone: 'undone',
  undo_failed: 'undo failed',
}

export function sinkInWords(b: PipelineRunBlock): string | null {
  if (b.undo_status === 'not_undoable') return 'delivered (irreversible) — could not be undone'
  if (!b.delivery) return null
  const where = locationInWords(b.delivery)
  switch (b.undo_status) {
    case 'undoing':
      return `undoing — delivered to ${where}`
    case 'undone':
      return `delivered, then undone — was ${where}`
    case 'undo_failed':
      return `undo failed: left in place — ${where}${b.undo_error ? `. ${b.undo_error}` : ''}`
    case 'kept':
      return `delivered to ${where} — kept: a webhook or email had already gone out`
    default:
      return `delivered to ${where}`
  }
}

// Deliveries rollback still has to undo: everything delivered and not yet settled.
export function undoLeft(run: PipelineRun): number {
  return run.blocks.filter(b => b.delivery && (b.undo_status === null || b.undo_status === 'undoing')).length
}

function approvalDetail(b: PipelineRunBlock): string | null {
  if (b.type !== 'approval' || !b.approval_deadline) return null
  const state = approvalState(b)
  if (state === 'waiting') return `waiting for approval — expires ${deadlineInWords(b.approval_deadline)}`
  const at = b.answered_at ? ` ${new Date(b.answered_at).toLocaleString()}` : ''
  if (b.status === 'waiting') return `${approvalStateInWords(state)}${at} — the run continues shortly`
  return `${approvalStateInWords(state)}${at}`
}

export function blockDetail(b: PipelineRunBlock): string {
  return (
    b.error ??
    approvalDetail(b) ??
    sinkInWords(b) ??
    (b.verdict ? verdictInWords(b.verdict) : null) ??
    skipReasonInWords(b.status_reason, b.reason_block_id) ??
    '—'
  )
}
