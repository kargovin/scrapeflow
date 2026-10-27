import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, type Job, type ScheduledJobsUsage } from '../api'
import { useApi } from '../lib/useApi'
import { describeCron, SKIP_REASONS } from '../lib/cron'
import StatusPill from './StatusPill'
import StoredResult from './StoredResult'

// F.7: a recurring job's schedule — what it is, when it next fires, and pause / resume (user mode).
export default function SchedulePanel({ job, mode }: { job: Job; mode: 'admin' | 'user' }) {
  const api = useApi()
  const qc = useQueryClient()
  const isUser = mode === 'user'

  // ADR-013's cap is only met where a schedule is armed: shown to the owner beside resume.
  const { data: usage } = useQuery<ScheduledJobsUsage>({
    queryKey: ['scheduled-jobs-usage'],
    queryFn: () => api.get('/users/me/scheduled-jobs'),
    enabled: isUser,
  })

  const setStatus = useMutation({
    mutationFn: (schedule_status: 'active' | 'paused') =>
      api.patch<Job>(`/jobs/${job.id}`, { schedule_status }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['job', mode, job.id] })
      qc.invalidateQueries({ queryKey: ['jobs'] })
      qc.invalidateQueries({ queryKey: ['scheduled-jobs-usage'] })
    },
  })

  if (!job.schedule_cron || !job.schedule_status) return null
  const active = job.schedule_status === 'active'
  const described = describeCron(job.schedule_cron)
  const capError =
    setStatus.error instanceof ApiError && setStatus.error.status === 429
      ? (setStatus.error.detail as { message?: string } | undefined)?.message
      : null

  const rows: [string, React.ReactNode][] = [
    [
      'Schedule',
      <span className="flex flex-col">
        <span>{described}</span>
        {described !== job.schedule_cron && (
          <span className="text-xs text-gray-400 font-mono">{job.schedule_cron} (UTC)</span>
        )}
      </span>,
    ],
    ['Next run', job.next_run_at ? new Date(job.next_run_at).toLocaleString() : '—'],
  ]

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
      <div className="px-4 py-3 flex items-center justify-between border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-900">
          Recurring schedule{' '}
          <span className="ml-1 text-xs font-normal text-gray-500">{active ? 'active' : 'paused'}</span>
        </h3>
        {isUser && (
          <button
            onClick={() => setStatus.mutate(active ? 'paused' : 'active')}
            disabled={setStatus.isPending}
            className="px-3 py-1 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
          >
            {setStatus.isPending ? 'Saving…' : active ? 'Pause' : 'Resume'}
          </button>
        )}
      </div>
      <dl className="divide-y divide-gray-100">
        {rows.map(([label, value]) => (
          <div key={label} className="px-4 py-2 sm:grid sm:grid-cols-3 sm:gap-4">
            <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
            <dd className="text-sm text-gray-900 sm:col-span-2">{value}</dd>
          </div>
        ))}
      </dl>
      {job.schedule_waiting_since && (
        <p className="px-4 py-2 text-xs text-amber-700 bg-amber-50 border-t border-amber-100">
          The current run is waiting for a free slot (all concurrent runs in use) since{' '}
          {new Date(job.schedule_waiting_since).toLocaleString()}.
        </p>
      )}
      {job.schedule_skipped_at && (
        <p className="px-4 py-2 text-xs text-gray-600 bg-gray-50 border-t border-gray-100">
          A run was skipped on {new Date(job.schedule_skipped_at).toLocaleString()}:{' '}
          {SKIP_REASONS[job.schedule_skip_reason ?? ''] ?? job.schedule_skip_reason}.
        </p>
      )}
      {isUser && usage && (
        <p className="px-4 py-2 text-xs text-gray-500 border-t border-gray-100">
          Active schedules: {usage.active} of {usage.limit}. Paused schedules do not count.
        </p>
      )}
      {capError && <p className="px-4 py-2 text-xs text-red-700 border-t border-red-100">{capError}</p>}
      {setStatus.isError && !capError && (
        <p className="px-4 py-2 text-xs text-red-700 border-t border-red-100">{String(setStatus.error)}</p>
      )}
      {isUser && <ScheduledRuns jobId={job.id} />}
    </div>
  )
}

// The job's runs, newest first — on a recurring job, its scheduled runs (and the first, run at
// creation). A tick that made no run is not here: it is the skip notice above (ADR-010 §1).
// Any completed run's output opens below the table, as a batch item's does.
function ScheduledRuns({ jobId }: { jobId: string }) {
  const api = useApi()
  const [viewing, setViewing] = useState<Job | null>(null)
  const { data: runs } = useQuery<Job[]>({
    queryKey: ['job-runs', jobId],
    queryFn: () => api.get(`/jobs/${jobId}/runs?limit=20`),
    refetchInterval: 30_000,
  })
  if (!runs?.length) return null
  const when = (run: Job) => new Date(run.run_created_at ?? run.created_at).toLocaleString()
  return (
    <div className="border-t border-gray-100">
      <h4 className="px-4 pt-3 text-xs font-medium text-gray-500 uppercase">Runs (latest 20)</h4>
      <table className="min-w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-400">
            <th className="px-4 py-1 text-left font-normal">Started</th>
            <th className="px-4 py-1 text-left font-normal">Finished</th>
            <th className="px-4 py-1 text-left font-normal">Status</th>
            <th className="px-4 py-1 text-left font-normal">Output</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {runs.map(run => (
            <tr key={run.run_id} className={viewing?.run_id === run.run_id ? 'bg-indigo-50' : ''}>
              <td className="px-4 py-2 text-gray-600">{when(run)}</td>
              <td className="px-4 py-2 text-gray-500">
                {run.completed_at ? new Date(run.completed_at).toLocaleString() : '—'}
              </td>
              <td className="px-4 py-2">
                <StatusPill status={run.status} />
                {run.error && (
                  <span className="ml-2 text-xs text-gray-500 truncate" title={run.error}>
                    {run.error}
                  </span>
                )}
              </td>
              <td className="px-4 py-2">
                {run.status === 'completed' && run.result_path ? (
                  <button
                    onClick={() => setViewing(viewing?.run_id === run.run_id ? null : run)}
                    className="text-xs text-indigo-600 hover:underline"
                  >
                    {viewing?.run_id === run.run_id ? 'hide' : 'view'}
                  </button>
                ) : (
                  <span className="text-xs text-gray-400">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {viewing && (
        <div className="p-4 border-t border-gray-100">
          <StoredResult
            path={`/jobs/${jobId}/runs/${viewing.run_id}/result`}
            title={`Run of ${when(viewing)}`}
            filename={`${jobId}-${viewing.run_id}`}
          />
        </div>
      )}
    </div>
  )
}
