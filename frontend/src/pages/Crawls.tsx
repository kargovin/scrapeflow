import { useState } from 'react'
import { useQuery, type Query } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import type { AdminUser, Crawl } from '../api'
import { useApi } from '../lib/useApi'
import Pager from '../components/Pager'
import StatusPill from '../components/StatusPill'

export const CRAWL_TERMINAL = new Set(['completed', 'failed', 'cancelled'])

export default function Crawls({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  const limit = 50

  const isAdmin = mode === 'admin'
  const apiBase = isAdmin ? '/admin/crawls' : '/crawls'
  const routeBase = isAdmin ? '/app/admin/crawls' : '/app/dashboard/crawls'

  const { data: users } = useQuery<AdminUser[]>({
    queryKey: ['admin-users'],
    queryFn: () => api.get('/admin/users?limit=200'),
    enabled: isAdmin,
    staleTime: 60_000,
  })

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (isAdmin && statusFilter) params.set('status', statusFilter)
  if (isAdmin && userFilter) params.set('user_id', userFilter)

  const { data: crawls, isLoading, isError } = useQuery<Crawl[]>({
    queryKey: ['crawls', mode, offset, statusFilter, userFilter],
    queryFn: () => api.get(`${apiBase}?${params}`),
    refetchInterval: (query: Query<Crawl[]>) =>
      (query.state.data ?? []).some(c => !CRAWL_TERMINAL.has(c.status)) ? 5_000 : false,
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Crawls</h2>
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
              {['queued', 'running', 'completed', 'failed', 'cancelled'].map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load crawls.</p>
      ) : !crawls?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">
          No crawls yet. Crawls are started through the API (<code>POST /crawls</code>).
        </p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Seed', ...(isAdmin ? ['User'] : []), 'Status', 'Pages', 'Engine', 'Created'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {crawls?.map(c => (
                  <tr
                    key={c.id}
                    onClick={() => navigate(`${routeBase}/${c.id}`)}
                    className="hover:bg-gray-50 cursor-pointer"
                  >
                    <td className="px-4 py-3 text-gray-900 max-w-xs truncate" title={c.seed_url}>{c.seed_url}</td>
                    {isAdmin && (
                      <td className="px-4 py-3 text-gray-500 max-w-[12rem] truncate" title={c.user_email ?? c.user_id}>
                        {c.user_email ?? c.user_id}
                      </td>
                    )}
                    <td className="px-4 py-3"><StatusPill status={c.status} /></td>
                    <td className="px-4 py-3 text-gray-500">
                      {c.total_completed} done · {c.total_failed} failed · {c.total_queued} found
                    </td>
                    <td className="px-4 py-3 text-gray-500">{c.engine}</td>
                    <td className="px-4 py-3 text-gray-500">{new Date(c.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={limit} count={crawls?.length} onChange={setOffset} />
        </>
      )}
    </div>
  )
}
