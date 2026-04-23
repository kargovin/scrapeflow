import { useQuery } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { apiGet, type AdminStats } from '../api'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4">
      <p className="text-xs text-gray-500 uppercase font-medium">{label}</p>
      <p className="text-2xl font-semibold text-gray-900 mt-1">{value}</p>
    </div>
  )
}

function Table({ rows }: { rows: [string, string | number][] }) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <table className="min-w-full text-sm divide-y divide-gray-100">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td className="px-4 py-2 text-gray-500 text-xs uppercase font-medium w-48">{k}</td>
              <td className="px-4 py-2 text-gray-900">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function UsageStats() {
  const { getToken } = useAuth()

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const { data: stats, isLoading, isError } = useQuery<AdminStats>({
    queryKey: ['admin-stats'],
    queryFn: () => apiGet('/admin/stats', token!),
    enabled: !!token,
    refetchInterval: 30_000,
  })

  if (isLoading || !token) return <p className="text-sm text-gray-500">Loading…</p>
  if (isError) return <p className="text-sm text-red-600">Failed to load stats.</p>

  const { operational: op, historical: hist } = stats!

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Live</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Running" value={op.jobs_running} />
          <Stat label="Pending" value={op.jobs_pending} />
          <Stat label="Webhooks pending" value={op.webhook_deliveries_pending} />
          <Stat label="Recurring jobs" value={op.active_recurring_jobs} />
        </div>
      </div>

      <div>
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Historical</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <Stat label="Jobs today" value={hist.jobs_today} />
          <Stat label="Jobs this week" value={hist.jobs_this_week} />
          <Stat label="Jobs this month" value={hist.jobs_this_month} />
          <Stat label="Storage used" value={formatBytes(hist.minio_storage_bytes)} />
        </div>
      </div>

      <div>
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Breakdown (7 days)</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <h3 className="text-sm font-medium text-gray-700 mb-2">By status</h3>
            <Table
              rows={Object.entries(hist.jobs_by_status_7d).sort((a, b) => Number(b[1]) - Number(a[1]))}
            />
          </div>
          <div>
            <h3 className="text-sm font-medium text-gray-700 mb-2">By engine</h3>
            <Table
              rows={Object.entries(hist.jobs_by_engine_7d).sort((a, b) => Number(b[1]) - Number(a[1]))}
            />
          </div>
        </div>
      </div>

      <div>
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Top users (7 days)</h2>
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Email</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Jobs</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {hist.top_users_by_jobs.map(u => (
                <tr key={u.user_id}>
                  <td className="px-4 py-2 text-gray-900">{u.email}</td>
                  <td className="px-4 py-2 text-right text-gray-600">{u.job_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
