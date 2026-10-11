import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type Pipeline } from '../api'
import { useApi } from '../lib/useApi'

export function chain(pipeline: Pipeline): string {
  return pipeline.definition.blocks.map(b => b.type).join(' → ')
}

export default function Pipelines({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const limit = 50

  const isAdmin = mode === 'admin'
  const routeBase = isAdmin ? '/app/admin/pipelines' : '/app/dashboard/pipelines'
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (isAdmin && includeDeleted) params.set('include_deleted', 'true')

  const { data: pipelines, isLoading, isError } = useQuery<Pipeline[]>({
    queryKey: ['pipelines', mode, offset, includeDeleted],
    queryFn: () => api.get(`${isAdmin ? '/admin/pipelines' : '/pipelines'}?${params}`),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Pipelines</h2>
        {isAdmin ? (
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={e => { setIncludeDeleted(e.target.checked); setOffset(0) }}
            />
            Include deleted
          </label>
        ) : (
          <button
            onClick={() => navigate(`${routeBase}/new`)}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
          >
            New pipeline
          </button>
        )}
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load pipelines.</p>
      ) : !pipelines?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">
          {isAdmin ? 'No pipelines.' : 'No pipelines yet — create one to chain scrape, clean, LLM, validate and webhook steps.'}
        </p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                  {isAdmin && (
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">User</th>
                  )}
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Steps</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Version</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {pipelines?.map(p => (
                  <tr
                    key={p.id}
                    onClick={() => navigate(`${routeBase}/${p.id}`)}
                    className="hover:bg-gray-50 cursor-pointer"
                  >
                    <td className="px-4 py-3 text-gray-900">
                      {p.name}
                      {p.deleted_at && <span className="ml-2 text-xs text-gray-400">(deleted)</span>}
                    </td>
                    {isAdmin && (
                      <td className="px-4 py-3 text-gray-500 max-w-[12rem] truncate">
                        {p.user_email ?? p.user_id}
                      </td>
                    )}
                    <td className="px-4 py-3 text-gray-500">{chain(p)}</td>
                    <td className="px-4 py-3 text-gray-500">v{p.version}</td>
                    <td className="px-4 py-3 text-gray-500">{new Date(p.updated_at).toLocaleString()}</td>
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
              disabled={!pipelines || pipelines.length < limit}
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
