import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { useNavigate } from 'react-router-dom'
import { apiGet, type AdminJob } from '../api'
import type { Query } from '@tanstack/react-query'

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

const STATUS_COLOURS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  running: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-600',
  processing: 'bg-purple-100 text-purple-700',
}

export default function Jobs() {
  const { getToken } = useAuth()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const limit = 50

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (statusFilter) params.set('status', statusFilter)

  const { data: jobs, isLoading, isError } = useQuery<AdminJob[]>({
    queryKey: ['admin-jobs', offset, statusFilter],
    queryFn: () => apiGet(`/admin/jobs?${params}`, token!),
    enabled: !!token,
    refetchInterval: (query: Query<AdminJob[]>) =>
      (query.state.data ?? []).some(j => !TERMINAL.has(j.status)) ? 5_000 : false,
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Jobs</h2>
        <select
          value={statusFilter}
          onChange={e => { setStatusFilter(e.target.value); setOffset(0) }}
          className="text-sm border border-gray-300 rounded px-2 py-1"
        >
          <option value="">All statuses</option>
          {['pending', 'running', 'completed', 'failed', 'cancelled', 'processing'].map(s => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      {isLoading || !token ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load jobs.</p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">URL</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Engine</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {jobs?.map(job => (
                  <tr
                    key={job.id}
                    onClick={() => navigate(`/app/admin/jobs/${job.id}`)}
                    className="hover:bg-gray-50 cursor-pointer"
                  >
                    <td className="px-4 py-3 text-gray-900 max-w-xs truncate" title={job.url}>
                      {job.url}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex px-2 py-0.5 text-xs rounded-full ${STATUS_COLOURS[job.status] ?? 'bg-gray-100 text-gray-600'}`}>
                        {job.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500">{job.output_format}</td>
                    <td className="px-4 py-3 text-gray-500">
                      {new Date(job.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-3 flex gap-2">
            <button
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={offset === 0}
              className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-40"
            >
              Previous
            </button>
            <button
              onClick={() => setOffset(offset + limit)}
              disabled={!jobs || jobs.length < limit}
              className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  )
}
