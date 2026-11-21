import { ApiError, type DestinationTestFailed, type DestinationTestResult } from '../api'
import StatusPill from './StatusPill'

// ADR-015 §2d: each check in order; a failed check ends the test, a warning still saves.
export default function DestinationChecks({ result }: { result: DestinationTestResult }) {
  const warned = result.checks.filter(c => c.outcome === 'warning')
  return (
    <div className="mt-3 bg-white border border-gray-200 rounded p-3 text-sm">
      <p className={`mb-2 font-semibold ${result.passed ? 'text-green-700' : 'text-red-700'}`}>
        {result.passed
          ? warned.length
            ? 'Test passed, with a warning'
            : 'Test passed'
          : 'Test failed'}
      </p>
      {result.checks.length === 0 ? (
        <p className="text-xs text-gray-500">No check ran.</p>
      ) : (
        <ul className="space-y-1">
          {result.checks.map(c => (
            <li key={c.check} className="flex items-start gap-2">
              <StatusPill status={c.outcome} />
              <span className="text-gray-900 w-20 shrink-0">{c.check}</span>
              {c.message && <span className="text-gray-600 break-words">{c.message}</span>}
            </li>
          ))}
        </ul>
      )}
      {warned.some(c => c.check === 'delete') && (
        <p className="mt-2 border border-amber-200 bg-amber-50 rounded p-2 text-xs text-amber-800">
          These credentials cannot delete. You can still use this destination, but if a run fails after
          delivering here, rollback cannot remove what it wrote: the run will show it as left in place.
        </p>
      )}
    </div>
  )
}

// The checks a refused save came back with (422 destination_test_failed), if that is what this is.
export function failedChecks(error: unknown): DestinationTestResult | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null
  const detail = error.detail as Partial<DestinationTestFailed> | null
  if (!detail || detail.error !== 'destination_test_failed' || !Array.isArray(detail.checks)) return null
  return { passed: false, checks: detail.checks }
}
