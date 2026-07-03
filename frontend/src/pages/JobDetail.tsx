import { useState, useEffect, lazy, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiGet, apiDelete, API_BASE, type Job } from '../api'

// Lazy — pulls Monaco + react-markdown into a separate chunk so the rest of the
// admin panel (Users, Jobs list, Stats) doesn't download the editor.
const ResultViewer = lazy(() => import('../components/ResultViewer'))

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

const STATUS_COLOURS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  running: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-600',
  processing: 'bg-purple-100 text-purple-700',
}

export default function JobDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { jobId } = useParams<{ jobId: string }>()
  const { getToken } = useAuth()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [liveStatus, setLiveStatus] = useState<string | null>(null)
  const [wsLive, setWsLive] = useState(false)

  const isAdmin = mode === 'admin'
  const apiBase = isAdmin ? '/admin/jobs' : '/jobs'
  const routeBase = isAdmin ? '/app/admin/jobs' : '/app/dashboard/jobs'

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const { data: job, isLoading, isError } = useQuery<Job>({
    queryKey: ['job', mode, jobId],
    queryFn: () => apiGet(`${apiBase}/${jobId}`, token!),
    enabled: !!token,
  })

  // Admin: permanent delete (removes MinIO objects + DB row).
  const permanentDelete = useMutation({
    mutationFn: () => apiDelete(`/jobs/${jobId}?permanent=true`, token!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      navigate(routeBase)
    },
  })

  // User: soft cancel of an in-progress job.
  const cancelJob = useMutation({
    mutationFn: () => apiDelete(`/jobs/${jobId}`, token!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['job', mode, jobId] })
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  useEffect(() => {
    if (!token || !job || TERMINAL.has(job.status)) return

    const base = API_BASE || window.location.origin
    const wsBase = base.replace(/^https/, 'wss').replace(/^http/, 'ws')
    const ws = new WebSocket(`${wsBase}/jobs/${jobId}/watch?token=${encodeURIComponent(token)}`)

    ws.onopen = () => setWsLive(true)
    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data) as { status: string }
      setLiveStatus(msg.status)
      if (TERMINAL.has(msg.status)) {
        qc.invalidateQueries({ queryKey: ['job', mode, jobId] })
        setWsLive(false)
      }
    }
    ws.onclose = () => setWsLive(false)
    ws.onerror = () => ws.close()

    return () => {
      ws.close()
      setWsLive(false)
    }
  }, [token, jobId, job?.status, qc, mode])

  if (!jobId) return null

  if (isLoading || !token) {
    return <p className="text-sm text-gray-500">Loading…</p>
  }
  if (isError || !job) {
    return <p className="text-sm text-red-600">Job not found.</p>
  }

  const status = liveStatus ?? job.status

  const fields: [string, React.ReactNode][] = [
    ['ID', job.id],
    // User row is admin-only — on the user dashboard it's always self (redundant).
    ...(isAdmin
      ? [['User', job.user_email ? (
          <span className="flex flex-col">
            <span>{job.user_email}</span>
            <span className="text-xs text-gray-400">{job.user_id}</span>
          </span>
        ) : job.user_id] as [string, React.ReactNode]]
      : []),
    ['URL', job.url],
    ['Output format', job.output_format],
    ['Status', (
      <span className="flex items-center gap-2">
        <span className={`inline-flex px-2 py-0.5 text-xs rounded-full ${STATUS_COLOURS[status] ?? 'bg-gray-100 text-gray-600'}`}>
          {status}
        </span>
        {wsLive && (
          <span className="flex items-center gap-1 text-xs text-green-600">
            <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
            live
          </span>
        )}
      </span>
    )],
    ['Result path', job.result_path ?? '—'],
    ['Error', job.error ?? '—'],
    ['Created', new Date(job.created_at).toLocaleString()],
    ['Updated', new Date(job.updated_at).toLocaleString()],
  ]

  return (
    <div>
      <button
        onClick={() => navigate(routeBase)}
        className="mb-4 text-sm text-indigo-600 hover:underline"
      >
        ← Back to jobs
      </button>

      <h2 className="text-lg font-semibold text-gray-900 mb-4">Job detail</h2>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <dl className="divide-y divide-gray-100">
          {fields.map(([label, value]) => (
            <div key={label} className="px-4 py-3 sm:grid sm:grid-cols-3 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-2 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      <Suspense fallback={<p className="mb-6 text-sm text-gray-500">Loading result viewer…</p>}>
        <ResultViewer
          jobId={job.id}
          outputFormat={job.output_format}
          status={status}
          resultPath={job.result_path}
          token={token}
          mode={mode}
        />
      </Suspense>

      {isAdmin ? (
        <div className="border border-red-200 rounded-lg p-4 bg-red-50">
          <h3 className="text-sm font-semibold text-red-800 mb-1">Danger zone</h3>
          <p className="text-xs text-red-600 mb-3">
            Permanent delete removes all MinIO objects and the database row. This cannot be undone.
          </p>
          {!confirmDelete ? (
            <button
              onClick={() => setConfirmDelete(true)}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700"
            >
              Delete permanently
            </button>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={() => permanentDelete.mutate()}
                disabled={permanentDelete.isPending}
                className="px-3 py-1.5 text-sm bg-red-700 text-white rounded hover:bg-red-800 disabled:opacity-50"
              >
                {permanentDelete.isPending ? 'Deleting…' : 'Confirm delete'}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
              >
                Cancel
              </button>
            </div>
          )}
          {permanentDelete.isError && (
            <p className="mt-2 text-xs text-red-700">{String(permanentDelete.error)}</p>
          )}
        </div>
      ) : !TERMINAL.has(status) ? (
        <div className="border border-gray-200 rounded-lg p-4 bg-gray-50">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">Cancel job</h3>
          <p className="text-xs text-gray-600 mb-3">
            Stop this job. Any output already stored is kept; the job will be marked cancelled.
          </p>
          <button
            onClick={() => cancelJob.mutate()}
            disabled={cancelJob.isPending}
            className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancelJob.isPending ? 'Cancelling…' : 'Cancel job'}
          </button>
          {cancelJob.isError && (
            <p className="mt-2 text-xs text-red-700">{String(cancelJob.error)}</p>
          )}
        </div>
      ) : null}
    </div>
  )
}
