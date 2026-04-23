import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { apiGet, apiDelete, type AdminJob } from '../api'

interface Props {
  jobId: string
  onBack: () => void
}

export default function JobDetail({ jobId, onBack }: Props) {
  const { getToken } = useAuth()
  const qc = useQueryClient()
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const { data: job, isLoading, isError } = useQuery<AdminJob>({
    queryKey: ['admin-job', jobId],
    queryFn: () => apiGet(`/admin/jobs/${jobId}`, token!),
    enabled: !!token,
  })

  const permanentDelete = useMutation({
    mutationFn: () => apiDelete(`/jobs/${jobId}?permanent=true`, token!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-jobs'] })
      onBack()
    },
  })

  if (isLoading || !token) {
    return <p className="text-sm text-gray-500">Loading…</p>
  }
  if (isError || !job) {
    return <p className="text-sm text-red-600">Job not found.</p>
  }

  const fields: [string, string][] = [
    ['ID', job.id],
    ['User ID', job.user_id],
    ['URL', job.url],
    ['Engine', job.output_format],
    ['Status', job.status],
    ['Result path', job.result_path ?? '—'],
    ['Error', job.error ?? '—'],
    ['Created', new Date(job.created_at).toLocaleString()],
    ['Updated', new Date(job.updated_at).toLocaleString()],
  ]

  return (
    <div>
      <button
        onClick={onBack}
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
    </div>
  )
}
