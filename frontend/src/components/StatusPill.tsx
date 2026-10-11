const COLOURS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  running: 'bg-blue-100 text-blue-700',
  waiting: 'bg-amber-100 text-amber-700',
  processing: 'bg-purple-100 text-purple-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-600',
  cancelling: 'bg-orange-100 text-orange-700',
  skipped: 'bg-gray-100 text-gray-400',
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
export function runStatus(run: { status: string; cancel_requested_at: string | null }): string {
  return run.status === 'running' && run.cancel_requested_at ? 'cancelling' : run.status
}
