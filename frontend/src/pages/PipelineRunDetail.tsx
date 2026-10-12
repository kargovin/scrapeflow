import { lazy, Suspense, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  type OutputState,
  type PipelineRun,
  type PipelineRunCancel,
  type PipelineRunResult,
} from '../api'
import { useApi } from '../lib/useApi'
import { useLiveSocket } from '../lib/useLiveSocket'
import ErrorBox from '../components/ErrorBox'
import LiveBadge from '../components/LiveBadge'
import StatusPill, { runStatus } from '../components/StatusPill'

// Monaco in its own chunk, as on the job page.
const ContentViewer = lazy(() => import('../components/ContentViewer'))

const FORMAT_BY_EXT: Record<string, string> = { json: 'json', md: 'markdown', html: 'html' }

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

const OUTPUT_LABELS: Record<OutputState, string> = {
  none: '—',
  passed_through: 'passed on',
  stored: 'stored',
  collected: 'collected',
}

function duration(start: string | null, end: string | null): string {
  if (!start) return '—'
  const ms = (end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}

function seconds(n: number): string {
  if (n < 120) return `${n} s`
  if (n < 7200) return `${Math.round(n / 60)} min`
  return `${(n / 3600).toFixed(1)} h`
}

export default function PipelineRunDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { pipelineId, runId } = useParams<{ pipelineId: string; runId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/pipelines' : '/pipelines'}/${pipelineId}/runs/${runId}`
  const routeBase = isAdmin ? '/app/admin/pipelines' : '/app/dashboard/pipelines'
  const runKey = ['pipeline-run', mode, runId]
  const [viewing, setViewing] = useState<string | null>(null)
  const [cancelInfo, setCancelInfo] = useState<PipelineRunCancel | null>(null)

  const { data: run, isLoading, isError } = useQuery<PipelineRun>({
    queryKey: runKey,
    queryFn: () => api.get(apiBase),
    // The owner gets pushes over the socket; an admin (no socket) polls.
    refetchInterval: (query: Query<PipelineRun>) =>
      isAdmin && query.state.data?.status === 'running' ? 5_000 : false,
  })

  // Every message refetches the run, so the block list is always the row's, not a patch.
  const socket = useLiveSocket<{ type?: string; status?: string }>({
    path: !isAdmin && run?.status === 'running' ? `/pipelines/${pipelineId}/runs/${runId}/watch` : null,
    onMessage: msg => {
      if (msg.type !== 'timeout') qc.invalidateQueries({ queryKey: runKey })
    },
    isTerminal: msg => TERMINAL.has(msg.status ?? ''),
  })

  const blockParam = viewing ? `?block=${encodeURIComponent(viewing)}` : ''
  const showResult = viewing !== null || run?.status === 'completed'
  const output = useQuery<PipelineRunResult>({
    queryKey: ['pipeline-run-output', mode, runId, viewing],
    queryFn: () => api.get(`${apiBase}/result${blockParam}`),
    enabled: !!run && showResult,
    retry: false,
  })

  const cancel = useMutation({
    mutationFn: () => api.post<PipelineRunCancel>(`/pipelines/${pipelineId}/runs/${runId}/cancel`, {}),
    onSuccess: info => {
      setCancelInfo(info)
      qc.setQueryData(runKey, info.run)
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (isError || !run) return <p className="text-sm text-red-600">Run not found.</p>

  const status = runStatus(run)
  const cancelling = status === 'cancelling'

  return (
    <div>
      <button
        onClick={() => navigate(`${routeBase}/${pipelineId}`)}
        className="mb-4 text-sm text-indigo-600 hover:underline"
      >
        ← Back to pipeline
      </button>
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Run</h2>
        <StatusPill status={status} />
        <span className="text-xs text-gray-400">v{run.version}</span>
        {!isAdmin && <LiveBadge state={socket} />}
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <dl className="divide-y divide-gray-100">
          {(
            [
              ['ID', run.id],
              ...(isAdmin ? [['Owner', run.user_id ?? '—']] : []),
              ['Inputs', Object.keys(run.inputs).length ? JSON.stringify(run.inputs) : '—'],
              ['Started', new Date(run.created_at).toLocaleString()],
              ['Finished', run.finished_at ? new Date(run.finished_at).toLocaleString() : '—'],
              ['Result', run.result_path ?? '—'],
              ['Error', run.error ?? '—'],
            ] as [string, string][]
          ).map(([label, value]) => (
            <div key={label} className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      {cancelling && (
        <div className="border border-orange-200 bg-orange-50 rounded-lg p-4 mb-6 text-sm text-orange-800">
          <p className="font-semibold mb-1">Cancelling</p>
          {cancelInfo ? (
            <p>
              {cancelInfo.still_running.length
                ? `Waiting for ${cancelInfo.still_running.join(', ')} to finish — at most ${seconds(cancelInfo.upper_bound_seconds)}.`
                : 'Stopping before the next step starts.'}{' '}
              {cancelInfo.llm_billed && 'An LLM step was already called, so your key may have been billed. '}
              {!cancelInfo.workflow_notified &&
                'The workflow engine could not be reached; the run stops at its next step boundary.'}
            </p>
          ) : (
            <p>
              Cancel requested {new Date(run.cancel_requested_at!).toLocaleString()}. A running step is
              never cut off; the run stops when it finishes.
            </p>
          )}
        </div>
      )}

      <h3 className="text-sm font-semibold text-gray-900 mb-2">Steps</h3>
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              {['#', 'Step', 'Status', 'Took', 'Output', 'Error'].map(h => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {run.blocks.map(b => (
              <tr key={b.block_id} className={viewing === b.block_id ? 'bg-indigo-50' : ''}>
                <td className="px-4 py-3 text-gray-400">{b.position + 1}</td>
                <td className="px-4 py-3 text-gray-900">
                  <span className="font-medium">{b.type}</span>
                  <span className="ml-1 text-gray-400">{b.block_id}</span>
                </td>
                <td className="px-4 py-3"><StatusPill status={b.status} /></td>
                <td className="px-4 py-3 text-gray-500">{duration(b.started_at, b.finished_at)}</td>
                <td className="px-4 py-3 text-gray-500">
                  {b.output_state === 'stored' ? (
                    <button
                      onClick={() => setViewing(viewing === b.block_id ? null : b.block_id)}
                      className="text-indigo-600 hover:underline"
                    >
                      {viewing === b.block_id ? 'hide' : 'view'}
                    </button>
                  ) : (
                    <span title={b.collected_at ? `collected ${new Date(b.collected_at).toLocaleString()}` : undefined}>
                      {OUTPUT_LABELS[b.output_state]}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-500 max-w-sm break-words">{b.error ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showResult && (
        output.isLoading ? (
          <p className="mb-6 text-sm text-gray-500">Loading output…</p>
        ) : output.isError ? (
          <div className="mb-6"><ErrorBox error={output.error} /></div>
        ) : output.data ? (
          <Suspense fallback={<p className="mb-6 text-sm text-gray-500">Loading viewer…</p>}>
            <ContentViewer
              key={output.data.result_path}
              title={viewing ? `Output of ${viewing}` : 'Result'}
              subtitle={
                <span className="text-xs text-gray-400 truncate" title={output.data.result_path}>
                  {output.data.result_path}
                </span>
              }
              format={FORMAT_BY_EXT[output.data.result_path.split('.').pop() ?? ''] ?? 'text'}
              content={output.data.content}
              filename={`${runId}-${viewing ?? 'result'}`}
            />
          </Suspense>
        ) : null
      )}

      {!isAdmin && run.status === 'running' && !run.cancel_requested_at && (
        <div className="border border-gray-200 rounded-lg p-4 bg-gray-50">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">Cancel run</h3>
          <p className="text-xs text-gray-600 mb-3">
            The step that is running finishes first; steps after it are skipped. Outputs already
            stored are kept.
          </p>
          <button
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
            className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancel.isPending ? 'Cancelling…' : 'Cancel run'}
          </button>
          <ErrorBox error={cancel.error} />
        </div>
      )}
    </div>
  )
}
