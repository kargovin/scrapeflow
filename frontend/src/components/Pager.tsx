export default function Pager({
  offset,
  limit,
  count,
  onChange,
}: {
  offset: number
  limit: number
  count: number | undefined
  onChange: (offset: number) => void
}) {
  return (
    <div className="mt-3 flex gap-2">
      <button
        onClick={() => onChange(Math.max(0, offset - limit))}
        disabled={offset === 0}
        className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-40"
      >
        Previous
      </button>
      <button
        onClick={() => onChange(offset + limit)}
        disabled={count === undefined || count < limit}
        className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-40"
      >
        Next
      </button>
    </div>
  )
}
