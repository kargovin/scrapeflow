import { ApiError, type BlockProblem } from '../api'

function problems(error: unknown): BlockProblem[] | null {
  if (!(error instanceof ApiError) || error.status !== 422 || !Array.isArray(error.detail)) {
    return null
  }
  const list = error.detail as Array<Partial<BlockProblem> & { msg?: string; loc?: unknown[] }>
  // Our validator sends {block_id, message}; FastAPI's own body validation sends {loc, msg}.
  return list.map(p => ({
    block_id: p.block_id ?? (p.loc ? p.loc.filter(x => x !== 'body').join('.') || null : null),
    message: p.message ?? p.msg ?? '',
  }))
}

// A 422 lists every problem by block; anything else shows the API's detail or message.
// `subject` names a problem that belongs to no block.
export default function ErrorBox({ error, subject = 'pipeline' }: { error: unknown; subject?: string }) {
  if (!error) return null
  const list = problems(error)
  const text =
    error instanceof ApiError && typeof error.detail === 'string'
      ? error.detail
      : error instanceof ApiError && typeof (error.detail as { message?: unknown } | null)?.message === 'string'
        ? (error.detail as { message: string }).message
      : error instanceof ApiError && error.detail && typeof error.detail === 'object' && !list
        ? JSON.stringify(error.detail)
        : String(error)
  return (
    <div className="mt-3 border border-red-200 bg-red-50 rounded p-3 text-xs text-red-700">
      {list ? (
        <ul className="space-y-1">
          {list.map((p, i) => (
            <li key={i}>
              <span className="font-semibold">{p.block_id ?? subject}:</span> {p.message}
            </li>
          ))}
        </ul>
      ) : (
        text
      )}
    </div>
  )
}
