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
    throw new ApiError(res.status, body)
  }
  return res
}

// Keeps the status and the parsed `detail`, so a page can tell a 409 or a 422's block problems
// apart. The message format is the one every page already shows.
export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body}`)
    this.status = status
    try {
      this.detail = (JSON.parse(body) as { detail?: unknown }).detail
    } catch {
      this.detail = body
    }
  }
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
    // Jobs, batches and crawls; a pipeline's webhook is a block of its run (ADR-009 15e).
    job_lane_webhooks_pending: number
    job_lane_webhooks_exhausted: number
    active_recurring_jobs: number
    active_recurring_jobs_by_lane: Record<string, number>
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
    job_lane_webhook_success_rate_7d: number
  }
}

// ---------------------------------------------------------------------------
// Pipelines (PRD-016 layer A)
// ---------------------------------------------------------------------------

export interface PipelineBlock {
  id: string
  type: string
  input: string | null
  config_schema_version: number
  config: Record<string, unknown>
  // Names of the secret fields that are set; their values are never returned.
  secrets: string[]
}

export interface PipelineDefinition {
  inputs: Record<string, Record<string, unknown>>
  time_budget_seconds: number | null
  blocks: PipelineBlock[]
}

export interface Pipeline {
  id: string
  name: string
  version: number
  definition: PipelineDefinition
  created_at: string
  updated_at: string
  // Admin routes only.
  user_id?: string
  user_email?: string | null
  deleted_at?: string | null
}

export interface PipelineRunSummary {
  id: string
  pipeline_id: string
  pipeline_version_id: string
  version: number
  status: string
  cancel_requested_at: string | null
  inputs: Record<string, string>
  result_path: string | null
  error: string | null
  created_at: string
  finished_at: string | null
  user_id?: string
}

export type OutputState = 'none' | 'passed_through' | 'stored' | 'collected'

export interface PipelineRunBlock {
  block_id: string
  position: number
  type: string
  status: string
  input_ref: string | null
  output_ref: string | null
  output_state: OutputState
  error: string | null
  started_at: string | null
  finished_at: string | null
  collected_at: string | null
}

export interface PipelineRun extends PipelineRunSummary {
  blocks: PipelineRunBlock[]
}

export interface PipelineRunResult {
  content: string
  result_path: string
  block_id: string
}

export interface PipelineRunCancel {
  run: PipelineRun
  still_running: string[]
  upper_bound_seconds: number
  llm_billed: boolean
  workflow_notified: boolean
}

// A 422 from save or run: every problem, each naming its block.
export interface BlockProblem {
  block_id: string | null
  message: string
}

// ---------------------------------------------------------------------------
// Batches and crawls (E.10)
// ---------------------------------------------------------------------------

export interface Batch {
  id: string
  user_id: string
  // Admin routes only.
  user_email?: string | null
  status: string
  output_format: string
  engine: string
  total: number
  completed: number
  failed: number
  webhook_url: string | null
  respect_robots: boolean
  llm_config: Record<string, unknown> | null
  created_at: string
  completed_at: string | null
}

export interface BatchItem {
  id: string
  url: string
  status: string
  result_path: string | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface Crawl {
  id: string
  user_id: string
  user_email?: string | null
  seed_url: string
  status: string
  max_depth: number
  max_pages: number
  include_paths: string[] | null
  exclude_paths: string[] | null
  ignore_sitemap: boolean
  output_format: string
  engine: string
  webhook_url: string | null
  respect_robots: boolean
  total_queued: number
  total_completed: number
  total_failed: number
  // URLs the crawl refused to fetch, by reason.
  dropped: Record<string, number>
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface CrawlPage {
  id: string
  crawl_id: string
  url: string
  depth: number
  status: string
  result_path: string | null
  error: string | null
  created_at: string
  completed_at: string | null
}
