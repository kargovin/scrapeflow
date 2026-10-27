import { useState } from 'react'
import { useMutation, useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import type { Batch, BatchItem } from '../api'
import { useApi } from '../lib/useApi'
import { useLiveSocket } from '../lib/useLiveSocket'
import ErrorBox from '../components/ErrorBox'
import LiveBadge from '../components/LiveBadge'
import Pager from '../components/Pager'
import StatusPill from '../components/StatusPill'
import StoredResult from '../components/StoredResult'
import { BATCH_TERMINAL } from './Batches'

export default function BatchDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { batchId } = useParams<{ batchId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [viewing, setViewing] = useState<BatchItem | null>(null)
  const limit = 100

  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/batches' : '/batch'}/${batchId}`
  const routeBase = isAdmin ? '/app/admin/batches' : '/app/dashboard/batches'
  const batchKey = ['batch', mode, batchId]
  const itemsKey = ['batch-items', mode, batchId, offset]

  const active = (q: Query<Batch>) => !!q.state.data && !BATCH_TERMINAL.has(q.state.data.status)
  const { data: batch, isLoading, isError } = useQuery<Batch>({
    queryKey: batchKey,
    queryFn: () => api.get(apiBase),
    // The owner gets pushes over the socket; an admin (no socket) polls.
    refetchInterval: (q: Query<Batch>) => (isAdmin && active(q) ? 5_000 : false),
  })
  const items = useQuery<BatchItem[]>({
    queryKey: itemsKey,
    queryFn: () => api.get(`${apiBase}/items?limit=${limit}&offset=${offset}`),
    refetchInterval: isAdmin && batch && !BATCH_TERMINAL.has(batch.status) ? 5_000 : false,
  })

  // The payload carries absolute totals (E.1); the item rows are refetched, never patched.
  const socket = useLiveSocket<{ type?: string; status?: string }>({
    path: !isAdmin && batch && !BATCH_TERMINAL.has(batch.status) ? `/batch/${batchId}/watch` : null,
    onMessage: msg => {
      if (msg.type === 'timeout') return
      qc.invalidateQueries({ queryKey: batchKey })
      qc.invalidateQueries({ queryKey: ['batch-items', mode, batchId] })
    },
    isTerminal: msg => BATCH_TERMINAL.has(msg.status ?? ''),
  })

  const cancel = useMutation({
    mutationFn: () => api.del(`/batch/${batchId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: batchKey })
      qc.invalidateQueries({ queryKey: ['batch-items', mode, batchId] })
      qc.invalidateQueries({ queryKey: ['batches'] })
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (isError || !batch) return <p className="text-sm text-red-600">Batch not found.</p>

  const fields: [string, string][] = [
    ['ID', batch.id],
    ...(isAdmin ? ([['User', batch.user_email ?? batch.user_id]] as [string, string][]) : []),
    ['Items', `${batch.completed} completed · ${batch.failed} failed · ${batch.total} total`],
    ['Engine', `${batch.engine} → ${batch.output_format}`],
    ['LLM step', batch.llm_config ? String(batch.llm_config.model ?? 'yes') : '—'],
    ['Webhook', batch.webhook_url ?? '—'],
    ['Created', new Date(batch.created_at).toLocaleString()],
    ['Finished', batch.completed_at ? new Date(batch.completed_at).toLocaleString() : '—'],
  ]

  return (
    <div>
      <button onClick={() => navigate(routeBase)} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back to batches
      </button>
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Batch</h2>
        <StatusPill status={batch.status} />
        {!isAdmin && <LiveBadge state={socket} />}
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
        <dl className="divide-y divide-gray-100">
          {fields.map(([label, value]) => (
            <div key={label} className="px-4 py-3 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      <h3 className="text-sm font-semibold text-gray-900 mb-2">Items</h3>
      {items.isError ? (
        <ErrorBox error={items.error} />
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['URL', 'Status', 'Error', 'Finished', 'Output'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.data?.map(item => (
                  <tr key={item.id} className={viewing?.id === item.id ? 'bg-indigo-50' : ''}>
                    <td className="px-4 py-3 text-gray-900 max-w-xs truncate" title={item.url}>{item.url}</td>
                    <td className="px-4 py-3"><StatusPill status={item.status} /></td>
                    <td className="px-4 py-3 text-gray-500 max-w-xs break-words">{item.error ?? '—'}</td>
                    <td className="px-4 py-3 text-gray-500">
                      {item.completed_at ? new Date(item.completed_at).toLocaleTimeString() : '—'}
                    </td>
                    <td className="px-4 py-3">
                      {item.status === 'completed' && item.result_path ? (
                        <button
                          onClick={() => setViewing(viewing?.id === item.id ? null : item)}
                          className="text-indigo-600 hover:underline"
                        >
                          {viewing?.id === item.id ? 'hide' : 'view'}
                        </button>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} limit={limit} count={items.data?.length} onChange={setOffset} />
        </>
      )}

      {viewing && (
        <div className="mt-6">
          <StoredResult
            path={`${apiBase}/items/${viewing.id}/result`}
            title={`Output of ${viewing.url}`}
            filename={`${batch.id}-${viewing.id}`}
          />
        </div>
      )}

      {!isAdmin && !BATCH_TERMINAL.has(batch.status) && (
        <div className="mt-6 border border-gray-200 rounded-lg p-4 bg-gray-50">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">Cancel batch</h3>
          <p className="text-xs text-gray-600 mb-3">
            Items not yet started are cancelled; items in flight finish their step and are then
            dropped uncharged. Items already completed keep their output.
          </p>
          <button
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
            className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancel.isPending ? 'Cancelling…' : 'Cancel batch'}
          </button>
          <ErrorBox error={cancel.error} />
        </div>
      )}
    </div>
  )
}
