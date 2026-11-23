const COLOURS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  queued: 'bg-yellow-100 text-yellow-700',
  partial_failure: 'bg-orange-100 text-orange-700',
  running: 'bg-blue-100 text-blue-700',
  waiting: 'bg-amber-100 text-amber-700',
  processing: 'bg-purple-100 text-purple-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-600',
  cancelling: 'bg-orange-100 text-orange-700',
  skipped: 'bg-gray-100 text-gray-400',
  undoing: 'bg-orange-100 text-orange-700',
  undone: 'bg-gray-100 text-gray-600',
  'undo failed': 'bg-red-100 text-red-700',
  passed: 'bg-green-100 text-green-700',
  warning: 'bg-amber-100 text-amber-700',
  expired: 'bg-amber-100 text-amber-700',
}

export default function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex px-2 py-0.5 text-xs rounded-full ${COLOURS[status] ?? 'bg-gray-100 text-gray-600'}`}
    >
      {status}
    </span>
  )
}

// R3: a run whose cancel is requested is still running until its current block finishes.
// A rollback ignores a cancel (PRD-017 R3), so undoing wins.
export function runStatus(run: {
  status: string
  cancel_requested_at: string | null
  rollback_deadline?: string | null
}): string {
  if (run.status !== 'running') return run.status
  if (run.rollback_deadline) return 'undoing'
  return run.cancel_requested_at ? 'cancelling' : run.status
}
