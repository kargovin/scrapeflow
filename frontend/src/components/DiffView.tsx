import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { DiffSummary, PipelineRunDiff } from '../api'
import { useApi } from '../lib/useApi'
import ErrorBox from './ErrorBox'

const LINE_COLOURS: Record<string, string> = {
  '+': 'bg-green-50 text-green-800',
  '-': 'bg-red-50 text-red-800',
  '@': 'bg-indigo-50 text-indigo-700',
}

function shown(value: unknown): string {
  return value === undefined ? '—' : JSON.stringify(value)
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <pre className="text-xs font-mono overflow-auto max-h-96 border border-gray-200 rounded">
      {lines.map((line, i) => (
        <div key={i} className={`px-2 whitespace-pre-wrap ${LINE_COLOURS[line[0]] ?? 'text-gray-600'}`}>
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

function Changes({ diff }: { diff: DiffSummary }) {
  return (
    <table className="min-w-full text-xs border border-gray-200 rounded">
      <thead className="bg-gray-50">
        <tr>
          {['Path', 'Before', 'After'].map(h => (
            <th key={h} className="px-2 py-1 text-left font-medium text-gray-500">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-100 font-mono">
        {(diff.changes ?? []).map(c => (
          <tr key={c.path}>
            <td className="px-2 py-1 text-gray-800">{c.path}</td>
            <td className="px-2 py-1 bg-red-50 text-red-800 break-all">{c.change === 'added' ? '—' : shown(c.old)}</td>
            <td className="px-2 py-1 bg-green-50 text-green-800 break-all">{c.change === 'removed' ? '—' : shown(c.new)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Body({ diff }: { diff: DiffSummary }) {
  if (diff.status === 'baseline_gone') {
    return <p className="text-xs text-gray-500">Changed; the previous copy is no longer stored, so there is nothing to compare with.</p>
  }
  if (diff.status === 'unavailable') {
    return <p className="text-xs text-gray-500">Changed; the comparison could not be computed{diff.reason ? ` (${diff.reason})` : ''}.</p>
  }
  return diff.kind === 'page' ? <Lines lines={diff.lines ?? []} /> : <Changes diff={diff} />
}

// ADR-016 §6a: the bounded summary at once; the stored full comparison on request.
export default function DiffView({
  blockId,
  summary,
  apiBase,
}: {
  blockId: string
  summary: DiffSummary
  apiBase: string
}) {
  const api = useApi()
  const [full, setFull] = useState(false)
  const stored = useQuery<PipelineRunDiff>({
    queryKey: ['pipeline-run-diff', apiBase, blockId],
    queryFn: () => api.get(`${apiBase}/diff?block=${encodeURIComponent(blockId)}`),
    enabled: full,
    retry: false,
  })
  const counts =
    summary.kind === 'page'
      ? `+${summary.lines_added ?? 0} −${summary.lines_removed ?? 0} lines`
      : `${summary.paths_changed ?? 0} values changed`

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 mb-4">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-sm font-semibold text-gray-900">
          {summary.kind === 'page' ? 'Page' : 'Extraction'} changes{' '}
          <span className="font-normal text-gray-400">at {blockId}</span>
          {summary.status === 'ok' && <span className="ml-2 text-xs font-normal text-gray-500">{counts}</span>}
        </h4>
        {summary.status === 'ok' && summary.truncated && (
          <button onClick={() => setFull(!full)} className="text-xs text-indigo-600 hover:underline">
            {full ? 'show the summary' : 'show the full comparison'}
          </button>
        )}
      </div>
      {full && stored.isLoading ? (
        <p className="text-xs text-gray-500">Loading…</p>
      ) : full && stored.data ? (
        <Body diff={stored.data.diff} />
      ) : (
        <Body diff={summary} />
      )}
      {full && <ErrorBox error={stored.error} />}
      {!full && summary.truncated && <p className="mt-1 text-xs text-gray-400">Summary cut short.</p>}
    </div>
  )
}
