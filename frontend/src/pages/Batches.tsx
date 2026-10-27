import { useState } from 'react'
import { useQuery, type Query } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import type { AdminUser, Batch } from '../api'
import { useApi } from '../lib/useApi'
import Pager from '../components/Pager'
import StatusPill from '../components/StatusPill'

export const BATCH_TERMINAL = new Set(['completed', 'partial_failure', 'cancelled'])

export default function Batches({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  const limit = 50

  const isAdmin = mode === 'admin'
  const apiBase = isAdmin ? '/admin/batches' : '/batch'
  const routeBase = isAdmin ? '/app/admin/batches' : '/app/dashboard/batches'

  const { data: users } = useQuery<AdminUser[]>({
    queryKey: ['admin-users'],
    queryFn: () => api.get('/admin/users?limit=200'),
    enabled: isAdmin,
    staleTime: 60_000,
  })

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (isAdmin && statusFilter) params.set('status', statusFilter)
  if (isAdmin && userFilter) params.set('user_id', userFilter)

  const { data: batches, isLoading, isError } = useQuery<Batch[]>({
    queryKey: ['batches', mode, offset, statusFilter, userFilter],
    queryFn: () => api.get(`${apiBase}?${params}`),
    refetchInterval: (query: Query<Batch[]>) =>
      (query.state.data ?? []).some(b => !BATCH_TERMINAL.has(b.status)) ? 5_000 : false,
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Batches</h2>
        {isAdmin && (
          <div className="flex items-center gap-2">
            <select
              value={userFilter}
              onChange={e => { setUserFilter(e.target.value); setOffset(0) }}
              className="text-sm border border-gray-300 rounded px-2 py-1 max-w-[16rem]"
            >
              <option value="">All users</option>
              {users?.map(u => <option key={u.id} value={u.id}>{u.email}</option>)}
            </select>
            <select
              value={statusFilter}
              onChange={e => { setStatusFilter(e.target.value); setOffset(0) }}
              className="text-sm border border-gray-300 rounded px-2 py-1"
            >
              <option value="">All statuses</option>
              {['queued', 'running', 'completed', 'partial_failure', 'cancelled'].map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load batches.</p>
      ) : !batches?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">
          No batches yet. Batches are created through the API (<code>POST /batch</code>) or MCP.
        </p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Batch', ...(isAdmin ? ['User'] : []), 'Status', 'Items', 'Engine', 'Created'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {batches?.map(b => (
                  <tr
                    key={b.id}
                    onClick={() => navigate(`${routeBase}/${b.id}`)}
                    className="hover:bg-gray-50 cursor-pointer"
                  >
                    <td className="px-4 py-3 text-gray-900 font-mono text-xs">{b.id.slice(0, 8)}</td>
                    {isAdmin && (
                      <td className="px-4 py-3 text-gray-500 max-w-[12rem] truncate" title={b.user_email ?? b.user_id}>
                        {b.user_email ?? b.user_id}
                      </td>
                    )}
                    <td className="px-4 py-3"><StatusPill status={b.status} /></td>
                    <td className="px-4 py-3 text-gray-500">
                      {b.completed} done · {b.failed} failed · {b.total} total
                    </td>
                    <td className="px-4 py-3 text-gray-500">{b.engine} → {b.output_format}</td>
                    <td className="px-4 py-3 text-gray-500">{new Date(b.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={limit} count={batches?.length} onChange={setOffset} />
        </>
      )}
    </div>
  )
}
