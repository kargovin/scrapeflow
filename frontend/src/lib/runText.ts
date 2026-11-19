import type { BlockStatusReason, PipelineRun, PipelineRunBlock, RunVerdict } from '../api'

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
    case 'not_recorded':
      return 'skipped — reason not recorded'
    default:
      return null
  }
}

// The run list's label: from the run row alone, no block reads (PRD-019 D9).
export function stopLabel(run: { stopped_at_block_id: string | null }): string | null {
  return run.stopped_at_block_id ? `stopped at ${run.stopped_at_block_id}` : null
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

export function blockDetail(b: PipelineRunBlock): string {
  return b.error ?? (b.verdict ? verdictInWords(b.verdict) : null) ?? skipReasonInWords(b.status_reason, b.reason_block_id) ?? '—'
}
