import { lazy, Suspense, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  type Destination,
  type OutputState,
  type PipelineRun,
  type PipelineRunAlert,
  type PipelineRunCancel,
  type PipelineRunResult,
  type RollbackEntry,
} from '../api'
import { useApi } from '../lib/useApi'
import { useWaitingApprovals } from '../lib/useApprovals'
import { useLiveSocket } from '../lib/useLiveSocket'
import {
  UNDO_PILL,
  blockDetail,
  failedBlock,
  locationInWords,
  approvalStopInWords,
  resultDeletedOnStop,
  undoLeft,
  verdictInWords,
} from '../lib/runText'
import { changeLabel, comparedWith } from '../lib/monitorText'
import ApprovalPanel from '../components/ApprovalPanel'
import DiffView from '../components/DiffView'
import ErrorBox from '../components/ErrorBox'
import Screenshots from '../components/Screenshots'
import LiveBadge from '../components/LiveBadge'
import StatusPill, { runStatus } from '../components/StatusPill'
import { LoadFailed, RefreshFailed } from '../components/LoadError'

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

// "by 14:20", with the date when it is not today.
function byTime(iso: string): string {
  const at = new Date(iso)
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return at.toDateString() === new Date().toDateString() ? time : `${at.toLocaleDateString()} ${time}`
}

const MESSAGE_EVENTS: Record<string, string> = {
  'pipeline.approval_requested': 'request',
  'pipeline.approval_expiring': 'reminder',
  'pipeline.approval_expired': 'expired',
  'pipeline.approval_superseded': 'superseded',
}

function rollbackLine(label: string, entries: RollbackEntry[]): string | null {
  return entries.length ? `${label}: ${entries.map(e => e.block_id).join(', ')}` : null
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
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { data: run, isLoading, error, refetch } = useQuery<PipelineRun>({
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

  const destinationIds = new Set([
    ...(run?.blocks.flatMap(b => (b.delivery ? [b.delivery.destination_id] : [])) ?? []),
    ...[...(run?.alerts ?? []), ...(run?.approval_messages ?? [])].flatMap(a =>
      a.destination_id ? [a.destination_id] : [],
    ),
  ])

  // "Unchanged since the check waiting for approval" (ADR-016 §9e): that run is the
  // monitor's waiting approval, while it still waits.
  const matchedWaiting = !!run?.blocks.some(b => b.verdict?.matched === 'waiting')
  const { data: waitingApprovals } = useWaitingApprovals({
    mode,
    monitorId: run?.monitor_id,
    enabled: matchedWaiting && !!run?.monitor_id,
  })
  const waitingRun = waitingApprovals?.find(a => a.run_id !== run?.id)
  // Names for the destinations this run wrote to; the run carries ids only.
  const { data: destinations } = useQuery<Destination[]>({
    queryKey: ['destinations', mode, isAdmin ? run?.user_id : 'me'],
    queryFn: () =>
      api.get(isAdmin ? `/admin/destinations?user_id=${run!.user_id}&limit=200` : '/destinations'),
    enabled: !!run && destinationIds.size > 0 && (!isAdmin || !!run.user_id),
  })
  const destinationBase = isAdmin ? '/app/admin/destinations' : '/app/dashboard/destinations'
  const destinationLink = (id: string | null) => {
    if (!id) return <span className="text-gray-400">—</span>
    const d = destinations?.find(x => x.id === id)
    if (destinations && !d) return <span className="text-gray-400">deleted</span>
    return (
      <button onClick={() => navigate(`${destinationBase}/${id}`)} className="text-indigo-600 hover:underline">
        {d ? `${d.name} (${d.display_host})` : id.slice(0, 8)}
      </button>
    )
  }

  const blockParam = viewing ? `?block=${encodeURIComponent(viewing)}` : ''
  const showResult = viewing !== null || (run?.status === 'completed' && !!run.result_path)
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

  const remove = useMutation({
    mutationFn: () => api.del(`/pipelines/${pipelineId}/runs/${runId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pipeline-runs', mode, pipelineId] })
      navigate(`${routeBase}/${pipelineId}`)
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (!run) return <LoadFailed error={error} subject="Run" onRetry={refetch} />

  const approvalBlock = run.blocks.find(b => b.type === 'approval')
  const awaitingApproval = approvalBlock?.status_reason === 'awaiting_approval' && !approvalBlock.approval_answer
  const status = awaitingApproval && runStatus(run) === 'running' ? 'waiting' : runStatus(run)
  const cancelling = status === 'cancelling'
  const undoing = status === 'undoing'
  const delivered = run.blocks.filter(b => b.delivery || b.undo_status)
  const reported: PipelineRunAlert | undefined = run.alerts.find(a => a.rollback)
  const stopper = run.blocks.find(b => b.block_id === run.stopped_at_block_id)
  const brokeAt = failedBlock(run)
  const refetchRun = () => {
    qc.invalidateQueries({ queryKey: runKey })
    qc.invalidateQueries({ queryKey: ['approvals'] })
  }
  const openRun = (id: string) => navigate(`${routeBase}/${pipelineId}/runs/${id}`)

  const messageTable = (list: PipelineRunAlert[], withEvent: boolean) => (
    <table className="min-w-full divide-y divide-gray-200 text-sm">
      <thead className="bg-gray-50">
        <tr>
          {[...(withEvent ? ['Message'] : []), 'Sent by', 'To', 'Status', 'Attempts', 'Last error'].map(h => (
            <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-100">
        {list.map(a => (
          <tr key={`${a.block_id}-${a.event}`}>
            {withEvent && <td className="px-4 py-3 text-gray-900">{MESSAGE_EVENTS[a.event] ?? a.event}</td>}
            <td className="px-4 py-3 text-gray-900">{a.block_id}</td>
            <td className="px-4 py-3 text-gray-500">
              {a.kind}
              {a.destination_id && <span className="ml-1">{destinationLink(a.destination_id)}</span>}
            </td>
            <td className="px-4 py-3">
              <StatusPill status={a.status === 'exhausted' ? 'failed' : a.status === 'delivered' ? 'completed' : 'pending'} />
              <span className="ml-2 text-xs text-gray-500">
                {a.status === 'exhausted'
                  ? 'gave up — undelivered'
                  : a.status === 'delivered'
                    ? `delivered ${new Date(a.delivered_at!).toLocaleString()}`
                    : `next try ${new Date(a.next_attempt_at).toLocaleString()}`}
              </span>
            </td>
            <td className="px-4 py-3 text-gray-500">{a.attempts}</td>
            <td className="px-4 py-3 text-gray-500 max-w-sm break-words">{a.last_error ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )

  return (
    <div>
      <RefreshFailed error={error} onRetry={refetch} />
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
              ...(run.stopped_at_block_id
                ? [
                    [
                      'Stopped at',
                      `${run.stopped_at_block_id}${
                        stopper?.verdict
                          ? ` — ${verdictInWords(stopper.verdict)}`
                          : approvalStopInWords(run.stop_reason)
                            ? ` — ${approvalStopInWords(run.stop_reason)}`
                            : ''
                      }`,
                    ],
                  ]
                : []),
              ...(brokeAt ? [['Failed at', brokeAt]] : []),
              ['Result', resultDeletedOnStop(run) ? 'result deleted when the run stopped' : (run.result_path ?? '—')],
              [run.status === 'failed' ? 'Error' : 'Note', run.error ?? '—'],
            ] as [string, string][]
          ).map(([label, value]) => (
            <div key={label} className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      {approvalBlock && (
        <ApprovalPanel run={run} block={approvalBlock} mode={mode} onAnswered={refetchRun} onOpenRun={openRun} />
      )}

      {run.change_status && (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
          <dl className="divide-y divide-gray-100">
            {run.monitor_id && (
              <div className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
                <dt className="text-xs font-medium text-gray-500 uppercase">Monitor</dt>
                <dd className="text-sm sm:col-span-3">
                  <button
                    onClick={() => navigate(`${isAdmin ? '/app/admin' : '/app/dashboard'}/monitors/${run.monitor_id}`)}
                    className="text-indigo-600 hover:underline"
                  >
                    open the monitor
                  </button>
                </dd>
              </div>
            )}
            <div className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">Change</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3">
                {changeLabel(run)}
                {matchedWaiting ? (
                  <span className="ml-1 text-gray-500">
                    since{' '}
                    {waitingRun ? (
                      <button onClick={() => openRun(waitingRun.run_id)} className="text-indigo-600 hover:underline">
                        the check waiting for approval ({waitingRun.run_id.slice(0, 8)})
                      </button>
                    ) : (
                      'the check that was waiting for approval'
                    )}
                    ; the approval is left as it was
                  </span>
                ) : comparedWith(run) && (
                  <span className="ml-1 text-gray-500">
                    {run.change_status === 'unchanged' ? 'since' : 'compared with'}{' '}
                    {run.baseline_run_id ? (
                      <button
                        onClick={() => navigate(`${routeBase}/${pipelineId}/runs/${run.baseline_run_id}`)}
                        className="text-indigo-600 hover:underline"
                      >
                        the check of {run.baseline_run_id.slice(0, 8)}
                      </button>
                    ) : (
                      comparedWith(run)
                    )}
                  </span>
                )}
                {run.change_status === 'unchanged' && run.result_path === null && (
                  <span className="block text-xs text-gray-500">This check kept no copy: the page was the same.</span>
                )}
              </dd>
            </div>
            {run.carried_sources && Object.keys(run.carried_sources).length > 0 && (
              <div className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
                <dt className="text-xs font-medium text-gray-500 uppercase">Carried values</dt>
                <dd className="text-sm text-gray-900 sm:col-span-3 space-y-1">
                  {Object.entries(run.carried_sources).map(([name, source]) => (
                    <div key={name}>
                      <span className="font-mono">{name} = {JSON.stringify(run.inputs[name])}</span>
                      <span className="ml-2 text-xs text-gray-500">
                        {source.from === 'baseline'
                          ? 'from the previous check'
                          : `the start value — ${source.why ?? 'nothing to carry'}`}
                      </span>
                    </div>
                  ))}
                </dd>
              </div>
            )}
          </dl>
        </div>
      )}

      {run.change_summary &&
        Object.entries(run.change_summary).map(([blockId, summary]) => (
          <DiffView key={blockId} blockId={blockId} summary={summary} apiBase={apiBase} />
        ))}

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

      {undoing && (
        <div className="border border-orange-200 bg-orange-50 rounded-lg p-4 mb-6 text-sm text-orange-800">
          <p className="font-semibold mb-1">
            Undoing deliveries — {undoLeft(run)} left, by {byTime(run.rollback_deadline!)}
          </p>
          <p>
            {brokeAt ?? 'A step'} failed, so what this run delivered is being removed, newest first.
            The run reads failed when that is done. A cancel does not stop it.
          </p>
        </div>
      )}

      <h3 className="text-sm font-semibold text-gray-900 mb-2">Steps</h3>
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              {['#', 'Step', 'Status', 'Took', 'Output', 'Details'].map(h => (
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
                <td className="px-4 py-3 space-x-1 whitespace-nowrap">
                  <StatusPill status={b.status} />
                  {b.undo_status && UNDO_PILL[b.undo_status] && <StatusPill status={UNDO_PILL[b.undo_status]} />}
                </td>
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
                <td className="px-4 py-3 text-gray-500 max-w-sm break-words">{blockDetail(b)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {delivered.length > 0 && (
        <>
          <h3 className="text-sm font-semibold text-gray-900 mb-2">Deliveries</h3>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Step', 'Destination', 'Where', 'State'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {delivered.map(b => (
                  <tr key={b.block_id}>
                    <td className="px-4 py-3 text-gray-900">{b.block_id}</td>
                    <td className="px-4 py-3">{destinationLink(b.delivery?.destination_id ?? null)}</td>
                    <td className="px-4 py-3 text-gray-700 font-mono text-xs break-all">
                      {b.delivery ? locationInWords(b.delivery) : 'sent — a message cannot be taken back'}
                    </td>
                    <td className="px-4 py-3 text-gray-500 max-w-sm break-words">
                      {b.undo_status === 'undone'
                        ? 'undone'
                        : b.undo_status === 'undoing'
                          ? 'undoing…'
                          : b.undo_status === 'undo_failed'
                            ? `undo failed: left in place — remove it by hand. ${b.undo_error ?? ''}`
                            : b.undo_status === 'kept'
                              ? 'kept: a webhook or email had already gone out'
                              : b.undo_status === 'not_undoable'
                                ? 'could not be undone'
                                : undoing
                                  ? 'waiting to be undone'
                                  : run.status === 'running'
                                    ? 'delivered — provisional until the run completes'
                                    : 'delivered'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {run.alerts.length > 0 && (
        <>
          <h3 className="text-sm font-semibold text-gray-900 mb-2">Failure alerts</h3>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
            {messageTable(run.alerts, false)}
            {reported?.rollback && (
              <p className="px-4 py-2 border-t border-gray-100 text-xs text-gray-600">
                The alert reports what rollback did —{' '}
                {[
                  rollbackLine('undone', reported.rollback.undone),
                  rollbackLine('left in place', reported.rollback.left_in_place),
                  rollbackLine('kept', reported.rollback.kept),
                ]
                  .filter(Boolean)
                  .join('; ') || 'nothing had been delivered'}
                .
              </p>
            )}
          </div>
        </>
      )}

      {run.approval_messages.length > 0 && (
        <>
          <h3 className="text-sm font-semibold text-gray-900 mb-2">Approval messages</h3>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
            {messageTable(run.approval_messages, true)}
          </div>
        </>
      )}

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
              footer={
                <Screenshots
                  base={`${apiBase}/blocks/${encodeURIComponent(output.data.block_id)}`}
                  screenshots={output.data.screenshots ?? []}
                  filename={`${runId}-${output.data.block_id}`}
                />
              }
            />
          </Suspense>
        ) : null
      )}

      {!isAdmin && run.status === 'running' && !run.cancel_requested_at && !undoing && (
        <div className="border border-gray-200 rounded-lg p-4 bg-gray-50">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">Cancel run</h3>
          <p className="text-xs text-gray-600 mb-3">
            {awaitingApproval
              ? 'The run is waiting for approval, so it ends cancelled at once; the request is closed. '
              : 'The step that is running finishes first; steps after it are skipped. '}
            Outputs already stored are kept.
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

      {!isAdmin && (
        <div className="mt-4 border border-red-200 rounded-lg p-4 bg-red-50">
          <h3 className="text-sm font-semibold text-red-800 mb-1">Delete run</h3>
          <p className="text-xs text-red-600 mb-3">
            The run and every output it stored are deleted, and the storage is freed.
            {run.status === 'running' && ' The run is cancelled first.'}
          </p>
          {!confirmDelete ? (
            <button
              onClick={() => setConfirmDelete(true)}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700"
            >
              Delete
            </button>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
                className="px-3 py-1.5 text-sm bg-red-700 text-white rounded hover:bg-red-800 disabled:opacity-50"
              >
                {remove.isPending ? 'Deleting…' : 'Confirm delete'}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
              >
                Keep
              </button>
            </div>
          )}
          <ErrorBox error={remove.error} />
        </div>
      )}
    </div>
  )
}
