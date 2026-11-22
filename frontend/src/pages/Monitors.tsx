import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type Monitor } from '../api'
import { useApi } from '../lib/useApi'
import { useWaitingApprovals } from '../lib/useApprovals'
import { describeCron } from '../lib/cron'
import { STATE_LABELS, changeLabel } from '../lib/monitorText'
import StatusPill from '../components/StatusPill'
import Pager from '../components/Pager'

export const STATE_PILL: Record<Monitor['state'], string> = {
  active: 'running',
  paused: 'cancelled',
  paused_itself: 'failed',
}

export default function Monitors({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const limit = 50
  const isAdmin = mode === 'admin'
  const routeBase = isAdmin ? '/app/admin/monitors' : '/app/dashboard/monitors'

  const { data: monitors, isLoading, isError } = useQuery<Monitor[]>({
    queryKey: ['monitors', mode, offset],
    queryFn: () => api.get(`${isAdmin ? '/admin/monitors' : '/monitors'}?limit=${limit}&offset=${offset}`),
    refetchInterval: 30_000,
  })
  // R9: the list shows each monitor's approvals waiting (at most one, D10).
  const { data: approvals } = useWaitingApprovals({ mode, refetchInterval: 30_000 })
  const waiting = (id: string) => approvals?.filter(a => a.monitor_id === id).length ?? 0

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Monitors</h2>
        {!isAdmin && (
          <button
            onClick={() => navigate(`${routeBase}/new`)}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
          >
            New monitor
          </button>
        )}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load monitors.</p>
      ) : !monitors?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">
          {isAdmin
            ? 'No monitors.'
            : 'No monitors yet — a monitor runs a pipeline on a schedule and tells you when the page changed.'}
        </p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Name', ...(isAdmin ? ['Owner'] : []), 'State', 'Schedule', 'Next check', 'Last check', 'Approval'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {monitors!.map(m => (
                  <tr key={m.id} onClick={() => navigate(`${routeBase}/${m.id}`)} className="hover:bg-gray-50 cursor-pointer">
                    <td className="px-4 py-3 text-gray-900 font-medium">{m.name}</td>
                    {isAdmin && <td className="px-4 py-3 text-gray-500">{m.user_email ?? m.user_id}</td>}
                    <td className="px-4 py-3">
                      <StatusPill status={STATE_PILL[m.state]} />
                      <span className="ml-2 text-xs text-gray-500">{STATE_LABELS[m.state]}</span>
                    </td>
                    <td className="px-4 py-3 text-gray-500">{describeCron(m.schedule_cron)}</td>
                    <td className="px-4 py-3 text-gray-500">
                      {m.next_check_at ? new Date(m.next_check_at).toLocaleString() : '—'}
                    </td>
                    <td className="px-4 py-3 text-gray-500">
                      {m.last_check ? (
                        <>
                          <StatusPill status={m.last_check.status} />
                          {changeLabel(m.last_check) && (
                            <span className="ml-2 text-xs">{changeLabel(m.last_check)}</span>
                          )}
                        </>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="px-4 py-3 text-gray-500">
                      {waiting(m.id) ? <StatusPill status="waiting" /> : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={limit} count={monitors?.length ?? 0} onChange={setOffset} />
        </>
      )}
    </div>
  )
}
