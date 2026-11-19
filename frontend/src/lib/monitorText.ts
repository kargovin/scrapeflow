import type { ChangeStatus, MonitorEvent, MonitorState, PipelineRun, PipelineRunSummary } from '../api'

export const STATE_LABELS: Record<MonitorState, string> = {
  active: 'active',
  paused: 'paused',
  paused_itself: 'paused itself',
}

// ADR-016 §10: "unchanged" is a stop at the first change gate, labelled from change_status.
const CHANGE_LABELS: Record<ChangeStatus, string> = {
  first_check: 'first check',
  no_baseline: 'no earlier check',
  changed: 'changed',
  unchanged: 'unchanged',
}

export function changeLabel(run: Pick<PipelineRunSummary, 'change_status'>): string | null {
  return run.change_status ? CHANGE_LABELS[run.change_status] : null
}

// R8: set, not a first check, and no baseline left = compared with a check since deleted.
export function comparedWith(run: Pick<PipelineRun, 'change_status' | 'baseline_run_id'>): string | null {
  if (!run.change_status || run.change_status === 'first_check' || run.change_status === 'no_baseline') {
    return null
  }
  return run.baseline_run_id ? run.baseline_run_id : 'a check since deleted'
}

// G1: identical firings are one counted line.
export function eventInWords(e: MonitorEvent): string {
  const times = e.count > 1 ? ` ×${e.count}` : ''
  const span =
    e.count > 1 ? ` (${new Date(e.first_at).toLocaleDateString()} – ${new Date(e.last_at).toLocaleDateString()})` : ''
  switch (e.kind) {
    case 'skipped_monthly_runs': {
      const resets = typeof e.detail?.resets_at === 'string' ? `; resets ${new Date(e.detail.resets_at).toLocaleDateString()}` : ''
      return `skipped${times} — monthly runs used up${span}${resets}`
    }
    case 'skipped_check_in_progress':
      return `skipped${times} — the previous check was still running${span}`
    case 'skipped_pipelines_disabled':
      return `skipped${times} — pipelines are switched off${span}`
    case 'waiting_for_slot':
      return `waited for a free run slot${e.ended_at ? '' : ' (still waiting)'}`
    case 'gave_up_waiting':
      return 'skipped — no run slot freed within a day'
    case 'paused_itself': {
      const error = typeof e.detail?.error === 'string' ? `: ${e.detail.error}` : ''
      return `paused itself after ${String(e.detail?.failures ?? 5)} failed checks in a row${error}`
    }
    default:
      return e.kind
  }
}
