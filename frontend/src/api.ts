export const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? ''

async function apiFetch(path: string, token: string, options?: RequestInit): Promise<Response> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...(options?.headers ?? {}),
    },
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${body}`)
  }
  return res
}

export async function apiGet<T>(path: string, token: string): Promise<T> {
  return (await apiFetch(path, token)).json()
}

export async function apiPatch<T>(path: string, token: string, body: unknown): Promise<T> {
  return (await apiFetch(path, token, { method: 'PATCH', body: JSON.stringify(body) })).json()
}

export async function apiPost<T>(path: string, token: string, body: unknown): Promise<T> {
  return (await apiFetch(path, token, { method: 'POST', body: JSON.stringify(body) })).json()
}

export async function apiDelete(path: string, token: string): Promise<void> {
  await apiFetch(path, token, { method: 'DELETE' })
}

// ---------------------------------------------------------------------------
// Response types (subset of API schemas needed by the SPA)
// ---------------------------------------------------------------------------

export interface AdminUser {
  id: string
  email: string
  is_admin: boolean
  created_at: string
}

export interface UserQuota {
  monthly_runs_limit: number | null
  concurrent_jobs_limit: number | null
  storage_bytes_limit: number | null
  storage_bytes_used: number
  updated_at: string
}

export interface Job {
  id: string
  user_id: string
  // Only populated on admin routes; user routes leave it null.
  user_email?: string | null
  url: string
  output_format: string
  engine?: string
  status: string
  result_path: string | null
  error: string | null
  created_at: string
  updated_at: string
  run_id: string
}

// Back-compat alias — the same shape is returned by both /admin/jobs and /jobs.
export type AdminJob = Job

export interface JobResult {
  content: string
  output_format: string
  result_path: string
  warnings: string[] | null
}

export interface ApiKey {
  id: string
  name: string
  created_at: string
  revoked: boolean
}

export interface ApiKeyCreated extends ApiKey {
  key: string
}

export interface AdminStats {
  operational: {
    jobs_running: number
    jobs_pending: number
    jobs_by_engine: Record<string, number>
    webhook_deliveries_pending: number
    webhook_deliveries_exhausted: number
    active_recurring_jobs: number
    next_scheduled_run_at: string | null
  }
  historical: {
    jobs_today: number
    jobs_this_week: number
    jobs_this_month: number
    jobs_by_status_7d: Record<string, number>
    jobs_by_engine_7d: Record<string, number>
    top_users_by_jobs: Array<{ user_id: string; email: string; job_count: number }>
    minio_storage_bytes: number
    webhook_delivery_success_rate_7d: number
  }
}
