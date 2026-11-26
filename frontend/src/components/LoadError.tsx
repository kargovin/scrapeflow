import { ApiError } from '../api'

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return typeof error.detail === 'string' && error.detail ? error.detail : `HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : String(error)
}

// The first load failed. "Not found" only for a 404 (BUG-033).
export function LoadFailed({
  error,
  subject,
  onRetry,
}: {
  error: unknown
  subject: string
  onRetry: () => void
}) {
  if (!error || (error instanceof ApiError && error.status === 404)) {
    return <p className="text-sm text-red-600">{subject} not found.</p>
  }
  return (
    <div className="text-sm text-red-600">
      <p>
        Could not load this {subject.toLowerCase()}: {errorText(error)}
      </p>
      <button onClick={() => onRetry()} className="mt-2 text-indigo-600 hover:underline">
        Try again
      </button>
    </div>
  )
}

// A refresh failed after the page had loaded: what is shown stays.
export function RefreshFailed({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (!error) return null
  return (
    <div className="mb-4 flex items-center gap-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
      <span>Could not refresh this page ({errorText(error)}). Showing what was loaded earlier.</span>
      <button onClick={() => onRetry()} className="underline hover:no-underline">
        Try again
      </button>
    </div>
  )
}
