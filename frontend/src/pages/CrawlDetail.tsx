import { useState } from 'react'
import { useMutation, useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import type { Crawl, CrawlPage } from '../api'
import { useApi } from '../lib/useApi'
import { useLiveSocket } from '../lib/useLiveSocket'
import ErrorBox from '../components/ErrorBox'
import LiveBadge from '../components/LiveBadge'
import Pager from '../components/Pager'
import StatusPill from '../components/StatusPill'
import StoredResult from '../components/StoredResult'
import { CRAWL_TERMINAL } from './Crawls'

// What each admission refusal means (ADR-009 13d, ADR-010 §2); a `sitemap_` prefix = a sitemap file.
const DROP_REASONS: Record<string, string> = {
  out_of_scope: 'outside the seed’s domain',
  ssrf: 'internal address',
  unresolved: 'host does not resolve',
  filtered: 'path filters',
  invalid: 'not an http(s) URL',
}

function dropLabel(reason: string): string {
  const file = reason.startsWith('sitemap_')
  const base = DROP_REASONS[file ? reason.slice('sitemap_'.length) : reason] ?? reason
  return file ? `sitemap file: ${base}` : base
}

export default function CrawlDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { crawlId } = useParams<{ crawlId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [pageStatus, setPageStatus] = useState('')
  const [viewing, setViewing] = useState<CrawlPage | null>(null)
  const limit = 100

  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/crawls' : '/crawls'}/${crawlId}`
  const routeBase = isAdmin ? '/app/admin/crawls' : '/app/dashboard/crawls'
  const crawlKey = ['crawl', mode, crawlId]

  const { data: crawl, isLoading, isError } = useQuery<Crawl>({
    queryKey: crawlKey,
    queryFn: () => api.get(apiBase),
    // The owner gets pushes over the socket; an admin (no socket) polls.
    refetchInterval: (q: Query<Crawl>) =>
      isAdmin && q.state.data && !CRAWL_TERMINAL.has(q.state.data.status) ? 5_000 : false,
  })
  const pageParams = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (pageStatus) pageParams.set('status', pageStatus)
  const pages = useQuery<CrawlPage[]>({
    queryKey: ['crawl-pages', mode, crawlId, offset, pageStatus],
    queryFn: () => api.get(`${apiBase}/pages?${pageParams}`),
    refetchInterval: isAdmin && crawl && !CRAWL_TERMINAL.has(crawl.status) ? 5_000 : false,
  })

  // Every message refetches the crawl and its pages: the counters are absolute, the rows the row's.
  const socket = useLiveSocket<{ type?: string; status?: string }>({
    path: !isAdmin && crawl && !CRAWL_TERMINAL.has(crawl.status) ? `/crawls/${crawlId}/watch` : null,
    onMessage: msg => {
      if (msg.type === 'timeout') return
      qc.invalidateQueries({ queryKey: crawlKey })
      qc.invalidateQueries({ queryKey: ['crawl-pages', mode, crawlId] })
    },
    isTerminal: msg => CRAWL_TERMINAL.has(msg.status ?? ''),
  })

  const cancel = useMutation({
    mutationFn: () => api.del(`/crawls/${crawlId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: crawlKey })
      qc.invalidateQueries({ queryKey: ['crawls'] })
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (isError || !crawl) return <p className="text-sm text-red-600">Crawl not found.</p>

  const drops = Object.entries(crawl.dropped ?? {})
  const fields: [string, React.ReactNode][] = [
    ['ID', crawl.id],
    ...(isAdmin ? ([['User', crawl.user_email ?? crawl.user_id]] as [string, string][]) : []),
    ['Seed', crawl.seed_url],
    ['Pages', `${crawl.total_completed} completed · ${crawl.total_failed} failed · ${crawl.total_queued} found (max ${crawl.max_pages}, depth ${crawl.max_depth})`],
    [
      'Refused',
      drops.length ? (
        <ul className="space-y-0.5">
          {drops.map(([reason, n]) => (
            <li key={reason}>
              {n} × {dropLabel(reason)}
            </li>
          ))}
        </ul>
      ) : (
        '—'
      ),
    ],
    ['Engine', crawl.engine],
    ['robots.txt', crawl.respect_robots ? 'respected' : 'ignored'],
    ['Sitemap', crawl.ignore_sitemap ? 'ignored' : 'followed'],
    ['Paths', [crawl.include_paths && `only ${crawl.include_paths.join(', ')}`, crawl.exclude_paths && `not ${crawl.exclude_paths.join(', ')}`].filter(Boolean).join('; ') || '—'],
    ['Note', crawl.error ?? '—'],
    ['Created', new Date(crawl.created_at).toLocaleString()],
    ['Finished', crawl.completed_at ? new Date(crawl.completed_at).toLocaleString() : '—'],
  ]

  return (
    <div>
      <button onClick={() => navigate(routeBase)} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back to crawls
      </button>
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Crawl</h2>
        <StatusPill status={crawl.status} />
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

      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-900">Pages</h3>
        <select
          value={pageStatus}
          onChange={e => { setPageStatus(e.target.value); setOffset(0) }}
          className="text-sm border border-gray-300 rounded px-2 py-1"
        >
          <option value="">All statuses</option>
          {['running', 'completed', 'failed', 'cancelled'].map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      {pages.isError ? (
        <ErrorBox error={pages.error} />
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['URL', 'Depth', 'Status', 'Error', 'Output'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {pages.data?.map(page => (
                  <tr key={page.id} className={viewing?.id === page.id ? 'bg-indigo-50' : ''}>
                    <td className="px-4 py-3 text-gray-900 max-w-xs truncate" title={page.url}>{page.url}</td>
                    <td className="px-4 py-3 text-gray-500">{page.depth}</td>
                    <td className="px-4 py-3"><StatusPill status={page.status} /></td>
                    <td className="px-4 py-3 text-gray-500 max-w-xs break-words">{page.error ?? '—'}</td>
                    <td className="px-4 py-3">
                      {page.status === 'completed' && page.result_path ? (
                        <button
                          onClick={() => setViewing(viewing?.id === page.id ? null : page)}
                          className="text-indigo-600 hover:underline"
                        >
                          {viewing?.id === page.id ? 'hide' : 'view'}
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
          <Pager offset={offset} limit={limit} count={pages.data?.length} onChange={setOffset} />
        </>
      )}

      {viewing && (
        <div className="mt-6">
          <StoredResult
            path={`${apiBase}/pages/${viewing.id}/result`}
            title={`Page ${viewing.url}`}
            filename={`${crawl.id}-${viewing.id}`}
          />
        </div>
      )}

      {!isAdmin && !CRAWL_TERMINAL.has(crawl.status) && (
        <div className="mt-6 border border-gray-200 rounded-lg p-4 bg-gray-50">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">Cancel crawl</h3>
          <p className="text-xs text-gray-600 mb-3">
            No new pages are fetched; pages in flight finish and are dropped uncharged. Pages already
            completed keep their output and stay charged until you delete the crawl.
          </p>
          <button
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
            className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancel.isPending ? 'Cancelling…' : 'Cancel crawl'}
          </button>
          <ErrorBox error={cancel.error} />
        </div>
      )}
    </div>
  )
}
