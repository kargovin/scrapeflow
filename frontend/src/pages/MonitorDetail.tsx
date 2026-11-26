import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  type AdminMonitorDetail,
  type Approval,
  type Destination,
  type Monitor,
  type MonitorEventKind,
  type MonitorNotice,
  type MonitorHistoryItem,
  type PipelineRun,
  type ScheduledJobsUsage,
} from '../api'
import { useApi } from '../lib/useApi'
import { useWaitingApprovals } from '../lib/useApprovals'
import { describeCron } from '../lib/cron'
import { STATE_LABELS, changeLabel, eventInWords } from '../lib/monitorText'
import { deadlineInWords, stopLabel } from '../lib/runText'
import ErrorBox from '../components/ErrorBox'
import StatusPill, { runStatus } from '../components/StatusPill'
import { STATE_PILL } from './Monitors'
import { LoadFailed, RefreshFailed } from '../components/LoadError'

// PRD-018 R6: a notice that could not be delivered is shown as such, beside its situation.
function NoticeState({ notice }: { notice: MonitorNotice }) {
  const to = notice.kind === 'email' ? 'email' : 'notice'
  if (notice.status === 'delivered') {
    return <span className="text-xs text-gray-500">{to} delivered {new Date(notice.delivered_at!).toLocaleString()}</span>
  }
  if (notice.status === 'exhausted') {
    return (
      <span className="text-xs text-red-600">
        {to} undelivered{notice.last_error ? ` — ${notice.last_error}` : ''}
      </span>
    )
  }
  return (
    <span className="text-xs text-amber-700">
      {to} sending{notice.attempts ? ` — tried ${notice.attempts}×${notice.last_error ? ` (${notice.last_error})` : ''}` : ''}, next try{' '}
      {new Date(notice.next_attempt_at).toLocaleString()}
    </span>
  )
}

const EVENT_PILL: Partial<Record<MonitorEventKind, string>> = {
  paused_itself: 'failed',
  approval_expired: 'expired',
}

function HistoryLine({
  item,
  waiting,
  onOpen,
}: {
  item: MonitorHistoryItem
  // The check's approval, while it waits for an answer.
  waiting: Approval | undefined
  onOpen: (runId: string) => void
}) {
  const at = new Date(item.at).toLocaleString()
  if (item.check) {
    const run = item.check
    const label = changeLabel(run)
    // An approval ending is a stop past the change gate: show both (ADR-016 §10).
    const approvalStop = run.stop_reason?.startsWith('approval_') ? stopLabel(run) : null
    return (
      <li
        onClick={() => onOpen(run.id)}
        className="px-4 py-2 flex items-center gap-3 hover:bg-gray-50 cursor-pointer text-sm"
      >
        <span className="w-44 text-xs text-gray-400">{at}</span>
        <StatusPill status={waiting && !run.cancel_requested_at ? 'waiting' : runStatus(run)} />
        <span className="text-gray-700">
          {label ?? (run.status === 'failed' ? run.error : stopLabel(run)) ?? ''}
          {label && approvalStop && <span className="ml-1 text-gray-500">· {approvalStop}</span>}
          {waiting?.deadline && (
            <span className="ml-1 text-amber-700">· waiting for approval, expires {deadlineInWords(waiting.deadline)}</span>
          )}
        </span>
      </li>
    )
  }
  const event = item.event!
  const runId = event.kind === 'approval_expired' && typeof event.detail?.run_id === 'string' ? event.detail.run_id : null
  return (
    <li
      onClick={runId ? () => onOpen(runId) : undefined}
      className={`px-4 py-2 flex items-center gap-3 text-sm bg-gray-50${runId ? ' hover:bg-gray-100 cursor-pointer' : ''}`}
    >
      <span className="w-44 text-xs text-gray-400">{at}</span>
      <StatusPill status={EVENT_PILL[event.kind] ?? 'skipped'} />
      <span className="text-gray-600">{eventInWords(event)}</span>
      {event.notice && <NoticeState notice={event.notice} />}
    </li>
  )
}

export default function MonitorDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { monitorId } = useParams<{ monitorId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()
  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/monitors' : '/monitors'}/${monitorId}`
  const routeBase = isAdmin ? '/app/admin/monitors' : '/app/dashboard/monitors'
  const pipelinesBase = isAdmin ? '/app/admin/pipelines' : '/app/dashboard/pipelines'
  const warnings = (location.state as { warnings?: string[] } | null)?.warnings ?? []
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<'plain' | 'with_checks' | null>(null)

  // R9: the page refreshes while it is open; no live channel.
  const { data: monitor, isLoading, error, refetch } = useQuery<Monitor | AdminMonitorDetail>({
    queryKey: ['monitor', mode, monitorId],
    queryFn: () => api.get(apiBase),
    refetchInterval: 10_000,
  })
  const { data: userHistory } = useQuery<MonitorHistoryItem[]>({
    queryKey: ['monitor-history', monitorId],
    queryFn: () => api.get(`${apiBase}/history?limit=100`),
    enabled: !isAdmin,
    refetchInterval: 10_000,
  })
  const history = isAdmin ? (monitor as AdminMonitorDetail | undefined)?.history : userHistory
  // ADR-016 §8: notices go to one webhook or smtp destination of the owner's.
  const { data: destinations } = useQuery<Destination[]>({
    queryKey: ['destinations', 'user', 'me'],
    queryFn: () => api.get('/destinations'),
    enabled: !isAdmin,
  })
  // ADR-016 §9e: at most one check waits for approval; later checks keep running.
  const { data: approvals } = useWaitingApprovals({ mode, monitorId, refetchInterval: 10_000 })
  const waitingApproval = approvals?.[0]
  const noticeId = monitor?.notice_destination_id ?? null
  const { data: adminDestination } = useQuery<Destination>({
    queryKey: ['destination', 'admin', noticeId],
    queryFn: () => api.get(`/admin/destinations/${noticeId}`),
    enabled: isAdmin && !!noticeId,
  })
  const noticeDestination = isAdmin ? adminDestination : destinations?.find(d => d.id === noticeId)
  const noticeChoices = destinations?.filter(d => d.kind === 'webhook' || d.kind === 'smtp') ?? []
  const { data: usage } = useQuery<ScheduledJobsUsage>({
    queryKey: ['scheduled-jobs-usage'],
    queryFn: () => api.get('/users/me/scheduled-jobs'),
    enabled: !isAdmin,
  })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['monitor', mode, monitorId] })
    qc.invalidateQueries({ queryKey: ['monitor-history', monitorId] })
    qc.invalidateQueries({ queryKey: ['monitors'] })
    qc.invalidateQueries({ queryKey: ['scheduled-jobs-usage'] })
  }
  const action = useMutation({
    mutationFn: (what: 'pause' | 'resume') => api.post<Monitor>(`/monitors/${monitorId}/${what}`, {}),
    onSuccess: refresh,
  })
  const checkNow = useMutation({
    mutationFn: () => api.post<PipelineRun>(`/monitors/${monitorId}/check`, {}),
    onSuccess: run => {
      refresh()
      navigate(`${pipelinesBase}/${run.pipeline_id}/runs/${run.id}`)
    },
  })
  const edit = useMutation({
    mutationFn: () =>
      api.patch<Monitor>(`/monitors/${monitorId}`, {
        name: name !== monitor?.name ? name : undefined,
        schedule_cron: cron !== monitor?.schedule_cron ? cron : undefined,
        notice_destination_id: notice !== (monitor?.notice_destination_id ?? '') ? notice || null : undefined,
      }),
    onSuccess: () => {
      setEditing(false)
      refresh()
    },
  })
  const remove = useMutation({
    mutationFn: (how: 'plain' | 'with_checks') =>
      api.del(`/monitors/${monitorId}${how === 'with_checks' ? '?with_checks=true' : ''}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['monitors'] })
      qc.invalidateQueries({ queryKey: ['scheduled-jobs-usage'] })
      navigate(routeBase)
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (!monitor) return <LoadFailed error={error} subject="Monitor" onRetry={refetch} />

  const active = monitor.state === 'active'
  const described = describeCron(monitor.schedule_cron)
  const rows: [string, React.ReactNode][] = [
    ...(isAdmin ? [['Owner', monitor.user_email ?? monitor.user_id ?? '—'] as [string, React.ReactNode]] : []),
    [
      'Pipeline',
      <button
        onClick={() => navigate(`${pipelinesBase}/${monitor.pipeline_id}`)}
        className="text-indigo-600 hover:underline"
      >
        v{monitor.version}
        {monitor.newer_version && (
          <span className="ml-2 text-xs text-gray-500">
            v{monitor.newer_version} exists — a monitor keeps its version; create a new one to use it
          </span>
        )}
      </button>,
    ],
    [
      'Schedule',
      <span className="flex flex-col">
        <span>{described}</span>
        {described !== monitor.schedule_cron && (
          <span className="text-xs text-gray-400 font-mono">{monitor.schedule_cron} (UTC)</span>
        )}
      </span>,
    ],
    [
      'Notices to',
      noticeId ? (
        <button
          onClick={() => navigate(`${isAdmin ? '/app/admin' : '/app/dashboard'}/destinations/${noticeId}`)}
          className="text-indigo-600 hover:underline"
        >
          {noticeDestination ? `${noticeDestination.name} (${noticeDestination.kind}, ${noticeDestination.display_host})` : noticeId}
        </button>
      ) : (
        <span className="text-gray-500">this page only — choose a webhook or email destination to be told elsewhere</span>
      ),
    ],
    ['Next check', monitor.next_check_at ? new Date(monitor.next_check_at).toLocaleString() : '—'],
    ['Failed in a row', `${monitor.consecutive_failures} (pauses itself at 5)`],
    ['Inputs', Object.keys(monitor.inputs).length ? JSON.stringify(monitor.inputs) : '—'],
    [
      'Carried',
      monitor.carried.length
        ? monitor.carried.map(c => `${c.input} ← ${c.from_block}${c.path} (starts at ${JSON.stringify(c.start)})`).join('; ')
        : '—',
    ],
  ]

  return (
    <div>
      <RefreshFailed error={error} onRetry={refetch} />
      <button onClick={() => navigate(routeBase)} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back to monitors
      </button>
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">{monitor.name}</h2>
        <StatusPill status={STATE_PILL[monitor.state]} />
        <span className="text-xs text-gray-500">{STATE_LABELS[monitor.state]}</span>
      </div>

      {warnings.length > 0 && (
        <ul className="mb-4 border border-amber-200 bg-amber-50 rounded p-3 text-xs text-amber-800 space-y-1">
          {warnings.map(w => <li key={w}>{w}</li>)}
        </ul>
      )}
      {waitingApproval && (
        <div className="mb-4 border border-amber-300 bg-amber-50 rounded p-3 text-xs text-amber-900">
          <p>
            <span className="font-semibold">
              A check is waiting for {isAdmin ? 'the owner’s' : 'your'} approval
            </span>
            {waitingApproval.deadline && ` — expires ${deadlineInWords(waitingApproval.deadline)}`}.{' '}
            <button
              onClick={() => navigate(`${pipelinesBase}/${waitingApproval.pipeline_id}/runs/${waitingApproval.run_id}`)}
              className="text-indigo-600 hover:underline"
            >
              {isAdmin ? 'Open the check' : 'Open the check to approve or reject'}
            </button>
          </p>
          <p className="mt-1">
            Checks keep running meanwhile: one that finds the same content leaves the approval
            waiting; one that finds other content closes it as superseded.
          </p>
        </div>
      )}
      {monitor.state === 'paused_itself' && (
        <p className="mb-4 border border-red-200 bg-red-50 rounded p-3 text-xs text-red-700">
          This monitor paused itself after five failed checks in a row. Fix what broke (see the
          history), then resume it.
        </p>
      )}

      {!isAdmin && (
        <div className="flex flex-wrap gap-2 mb-4">
          <button
            onClick={() => checkNow.mutate()}
            disabled={checkNow.isPending}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {checkNow.isPending ? 'Starting…' : 'Check now'}
          </button>
          <button
            onClick={() => action.mutate(active ? 'pause' : 'resume')}
            disabled={action.isPending}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
          >
            {action.isPending ? 'Saving…' : active ? 'Pause' : 'Resume'}
          </button>
          <button
            onClick={() => {
              setName(monitor.name)
              setCron(monitor.schedule_cron)
              setNotice(monitor.notice_destination_id ?? '')
              setEditing(!editing)
            }}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
          >
            Edit name, schedule or notices
          </button>
          {usage && (
            <span className="self-center text-xs text-gray-500">
              {usage.active} of {usage.limit} schedules: {usage.jobs} jobs, {usage.monitors} monitors
            </span>
          )}
        </div>
      )}
      <ErrorBox error={checkNow.error ?? action.error} />

      {editing && (
        <div className="bg-white border border-gray-200 rounded-lg p-4 mb-4 flex flex-wrap items-end gap-3">
          <label className="text-xs text-gray-500">
            name
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              className="block mt-1 w-64 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
            />
          </label>
          <label className="text-xs text-gray-500">
            schedule (cron, UTC)
            <input
              value={cron}
              onChange={e => setCron(e.target.value)}
              className="block mt-1 w-48 text-sm font-mono border border-gray-300 rounded px-2 py-1 text-gray-900"
            />
          </label>
          <label className="text-xs text-gray-500">
            notices also go to
            <select
              value={notice}
              onChange={e => setNotice(e.target.value)}
              className="block mt-1 w-64 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
            >
              <option value="">nowhere else (this page only)</option>
              {noticeChoices.map(d => (
                <option key={d.id} value={d.id}>{d.name} ({d.kind}, {d.display_host})</option>
              ))}
              {notice && !noticeChoices.some(d => d.id === notice) && <option value={notice}>{notice}</option>}
            </select>
          </label>
          <button
            onClick={() => edit.mutate()}
            disabled={edit.isPending || !name.trim() || !cron.trim()}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {edit.isPending ? 'Saving…' : 'Save'}
          </button>
          <ErrorBox error={edit.error} subject="monitor" />
        </div>
      )}

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <dl className="divide-y divide-gray-100">
          {rows.map(([label, value]) => (
            <div key={label} className="px-4 py-2 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      <h3 className="text-sm font-semibold text-gray-900 mb-2">History</h3>
      {!history?.length ? (
        <p className="text-sm text-gray-500 mb-6">No checks yet.</p>
      ) : (
        <ul className="bg-white border border-gray-200 rounded-lg divide-y divide-gray-100 mb-6">
          {history.map(item => (
            <HistoryLine
              key={item.check?.id ?? item.event!.id}
              item={item}
              waiting={approvals?.find(a => a.run_id === item.check?.id)}
              onOpen={runId => navigate(`${pipelinesBase}/${monitor.pipeline_id}/runs/${runId}`)}
            />
          ))}
        </ul>
      )}

      {!isAdmin && (
        <div className="border border-red-200 rounded-lg p-4 bg-red-50">
          <h3 className="text-sm font-semibold text-red-800 mb-1">Delete monitor</h3>
          <p className="text-xs text-red-600 mb-3">
            Delete: the monitor goes; its checks stay as ordinary runs of the pipeline. Delete with
            checks: every check and every result it stored goes too, and the storage is freed. A
            check still running is cancelled either way.
          </p>
          {confirmDelete === null ? (
            <div className="flex gap-2">
              <button
                onClick={() => setConfirmDelete('plain')}
                className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700"
              >
                Delete
              </button>
              <button
                onClick={() => setConfirmDelete('with_checks')}
                className="px-3 py-1.5 text-sm bg-red-700 text-white rounded hover:bg-red-800"
              >
                Delete with checks
              </button>
            </div>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={() => remove.mutate(confirmDelete)}
                disabled={remove.isPending}
                className="px-3 py-1.5 text-sm bg-red-700 text-white rounded hover:bg-red-800 disabled:opacity-50"
              >
                {remove.isPending
                  ? 'Deleting…'
                  : confirmDelete === 'with_checks'
                    ? 'Confirm: delete every check and result'
                    : 'Confirm delete'}
              </button>
              <button
                onClick={() => setConfirmDelete(null)}
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
