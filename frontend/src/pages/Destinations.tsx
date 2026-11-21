import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type Destination, type DestinationKind } from '../api'
import { useApi } from '../lib/useApi'
import { KINDS, KIND_LABELS } from '../lib/destinationForm'

export default function Destinations({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const isAdmin = mode === 'admin'
  const routeBase = isAdmin ? '/app/admin/destinations' : '/app/dashboard/destinations'
  const [kind, setKind] = useState<DestinationKind | ''>('')
  const [offset, setOffset] = useState(0)
  const limit = 50

  const params = new URLSearchParams()
  if (kind) params.set('kind', kind)
  if (isAdmin) {
    params.set('limit', String(limit))
    params.set('offset', String(offset))
  }
  const { data: destinations, isLoading, isError } = useQuery<Destination[]>({
    queryKey: ['destinations', mode, kind, offset],
    queryFn: () => api.get(`${isAdmin ? '/admin/destinations' : '/destinations'}?${params}`),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Destinations</h2>
        <div className="flex items-center gap-3">
          <select
            value={kind}
            onChange={e => {
              setKind(e.target.value as DestinationKind | '')
              setOffset(0)
            }}
            className="text-sm border border-gray-300 rounded px-2 py-1"
          >
            <option value="">All kinds</option>
            {KINDS.map(k => (
              <option key={k} value={k}>{KIND_LABELS[k]}</option>
            ))}
          </select>
          {!isAdmin && (
            <button
              onClick={() => navigate(`${routeBase}/new`)}
              className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
            >
              New destination
            </button>
          )}
        </div>
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load destinations.</p>
      ) : !destinations?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">
          {isAdmin
            ? 'No destinations.'
            : 'No destinations yet — save a bucket, webhook or mail server once, then point pipeline steps and monitor notices at it.'}
        </p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Name', ...(isAdmin ? ['User'] : []), 'Kind', 'Host', 'Updated'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {destinations?.map(d => (
                  <tr key={d.id} onClick={() => navigate(`${routeBase}/${d.id}`)} className="hover:bg-gray-50 cursor-pointer">
                    <td className="px-4 py-3 text-gray-900">{d.name}</td>
                    {isAdmin && (
                      <td className="px-4 py-3 text-gray-500 max-w-[12rem] truncate">{d.user_email ?? d.user_id}</td>
                    )}
                    <td className="px-4 py-3 text-gray-500">{KIND_LABELS[d.kind]}</td>
                    <td className="px-4 py-3 text-gray-500">{d.display_host}</td>
                    <td className="px-4 py-3 text-gray-500">{new Date(d.updated_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {isAdmin && (
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
                disabled={!destinations || destinations.length < limit}
                className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-40"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
