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
  scheduled_jobs_limit: number | null
  storage_bytes_used: number
  updated_at: string
}

// ADR-013's cap, shared by scheduled jobs and monitors (ADR-016 §12); `active` is their sum.
export interface ScheduledJobsUsage {
  active: number
  limit: number
  jobs: number
  monitors: number
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
  // The run's own creation time; created_at is the job's.
  run_created_at?: string | null
  completed_at?: string | null
  // F.7 — null on a job without a schedule; next_run_at is null while paused.
  schedule_cron: string | null
  schedule_status: 'active' | 'paused' | null
  next_run_at: string | null
  schedule_skipped_at: string | null
  schedule_skip_reason: string | null
  schedule_waiting_since: string | null
}

// Back-compat alias — the same shape is returned by both /admin/jobs and /jobs.
export type AdminJob = Job

export interface JobResult {
  content: string
  output_format: string
  result_path: string
  warnings: string[] | null
  // The job run it came from (job routes only).
  run_id?: string | null
  completed_at?: string | null
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
    active_monitors: number
    monitors_paused_itself: number
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
  // Single-pipeline reads: the monitors using it, which block its delete.
  monitors?: MonitorRef[] | null
}

export interface PipelineRunSummary {
  id: string
  pipeline_id: string
  pipeline_version_id: string
  version: number
  status: string
  cancel_requested_at: string | null
  inputs: Record<string, RunInputValue>
  result_path: string | null
  error: string | null
  created_at: string
  finished_at: string | null
  // How it ended (ADR-014 §5a): a stop is `completed` plus these two.
  stopped_at_block_id: string | null
  stop_reason: StopReason | null
  failed_block_id: string | null
  rollback_deadline: string | null
  user_id?: string
  // A monitor's check (ADR-016 §2b); null monitor_id = a hand-run, or its monitor is deleted.
  monitor_id: string | null
  change_status: ChangeStatus | null
}

export type ChangeStatus = 'first_check' | 'no_baseline' | 'changed' | 'unchanged'

// ADR-014 §5a; the approval endings are ADR-016 §10's.
export type StopReason = 'rule_not_met' | 'approval_rejected' | 'approval_expired' | 'approval_superseded'

export type RunInputValue = string | number

export type BlockStatusReason =
  | 'stopped'
  | 'upstream_failed'
  | 'cancelled'
  | 'not_started'
  | 'alert_only'
  | 'awaiting_approval'
  | 'not_recorded'

// A stop-mode Validate's verdict; `rule` is as saved, so a bound value is {"$input": name}.
export interface RunVerdict {
  rule_index: number
  rule: Record<string, unknown>
  found: unknown
  expected: unknown
  // A change gate's: which earlier check's content this one matched.
  matched?: 'baseline' | 'waiting' | null
}

// A failure alert (ADR-014 §8e) or an Approval block's message (ADR-016 §9c).
export interface PipelineRunAlert {
  block_id: string
  event: string
  kind: 'webhook' | 'email'
  // A saved destination's; null for an inline URL, or once that destination is deleted.
  destination_id: string | null
  status: 'pending' | 'delivered' | 'exhausted'
  attempts: number
  last_error: string | null
  next_attempt_at: string
  delivered_at: string | null
  created_at: string
  // What the alert says rollback did (PRD-017 R3).
  rollback: RollbackLists | null
}

export interface RollbackEntry {
  block_id: string
  kind: string
  location: BucketLocation | PostgresLocation | Record<string, unknown>
}

export interface RollbackLists {
  undone: RollbackEntry[]
  left_in_place: RollbackEntry[]
  kept: RollbackEntry[]
}

// Where a bucket sink wrote (ADR-015 §5): the location its undo uses, never the destination's current one.
export interface BucketLocation {
  endpoint: string
  bucket: string
  region: string | null
  key: string
}

// Where a postgres sink wrote (ADR-015 §5): the row's key in the table, as recorded at delivery.
export interface PostgresLocation {
  host: string
  port: number
  database: string
  schema: string
  table: string
  run_id: string
  block_id: string
}

export interface SinkDelivery {
  destination_id: string
  kind: string
  location: BucketLocation | PostgresLocation | Record<string, unknown>
}

// ADR-014 §5a: after a delivery, on a failed run. Null = nothing to undo.
export type UndoStatus = 'undoing' | 'undone' | 'undo_failed' | 'kept' | 'not_undoable'

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
  status_reason: BlockStatusReason | null
  reason_block_id: string | null
  verdict: RunVerdict | null
  // A sink's delivery record and its undo (PRD-017).
  delivery: SinkDelivery | null
  undo_status: UndoStatus | null
  undo_error: string | null
  // An Approval block (ADR-016 §9a); null on every other block. While the run is still
  // waiting, an answer can already be recorded: the workflow has not woken yet.
  approval_deadline: string | null
  approval_answer: ApprovalAnswer | null
  answered_at: string | null
  answered_by: string | null
  superseded_by_run_id: string | null
}

export type ApprovalAnswer = 'approved' | 'rejected' | 'expired' | 'superseded'

export interface PipelineRun extends PipelineRunSummary {
  blocks: PipelineRunBlock[]
  alerts: PipelineRunAlert[]
  // The Approval block's request, reminder, expired and superseded messages.
  approval_messages: PipelineRunAlert[]
  baseline_run_id: string | null
  // The bounded diff summaries, by the block that triggered each.
  change_summary: Record<string, DiffSummary> | null
  carried_sources: Record<string, CarriedSource> | null
}

// ADR-016 §6a: a page diff's unified lines, or an extraction's changed paths.
export interface DiffSummary {
  status: 'ok' | 'baseline_gone' | 'unavailable'
  kind: 'page' | 'extraction'
  reason?: string | null
  lines?: string[]
  lines_added?: number
  lines_removed?: number
  changes?: PathChange[]
  paths_changed?: number
  truncated?: boolean
}

export interface PathChange {
  path: string
  change: 'added' | 'removed' | 'changed'
  old: unknown
  new: unknown
}

export interface CarriedSource {
  from: 'baseline' | 'start'
  run_id?: string
  why?: string
}

export interface PipelineRunDiff {
  block_id: string
  diff_path: string
  diff: DiffSummary
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
// Approvals (ADR-016 §9 · PRD-018 R5, R9)
// ---------------------------------------------------------------------------

// The recorded answer, else where the block is.
export type ApprovalState = 'not_requested' | 'waiting' | ApprovalAnswer | 'closed'

export interface Approval {
  pipeline_id: string
  pipeline_name: string
  run_id: string
  block_id: string
  // A monitor's check; null for a hand-run.
  monitor_id: string | null
  run_status: string
  state: ApprovalState
  requested_at: string | null
  deadline: string | null
  answered_at: string | null
  answered_by: string | null
  superseded_by_run_id: string | null
  change_status: ChangeStatus | null
  change_summary: Record<string, DiffSummary> | null
  // What this run has already delivered: a reject keeps it.
  deliveries: Array<{
    block_id: string
    kind: string
    location: BucketLocation | PostgresLocation | Record<string, unknown>
  }>
  // Admin routes only.
  user_id?: string
  user_email?: string | null
}

// 409 from an answer: the approval as it stands.
export interface ApprovalRefused {
  message: string
  approval: Approval
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

// ---------------------------------------------------------------------------
// Monitors (PRD-018 · ADR-016)
// ---------------------------------------------------------------------------

export type MonitorState = 'active' | 'paused' | 'paused_itself'

export interface MonitorRef {
  id: string
  name: string
  state: MonitorState
}

export interface CarriedInput {
  input: string
  from_block: string
  path: string
  start: number | string
}

export interface Monitor {
  id: string
  name: string
  pipeline_id: string
  pipeline_version_id: string
  version: number
  newer_version: number | null
  inputs: Record<string, RunInputValue>
  carried: CarriedInput[]
  schedule_cron: string
  // Where notices also go: a webhook or smtp destination.
  notice_destination_id: string | null
  state: MonitorState
  consecutive_failures: number
  next_check_at: string | null
  last_check: {
    run_id: string
    status: string
    change_status: ChangeStatus | null
    stop_reason: string | null
    created_at: string
    finished_at: string | null
  } | null
  created_at: string
  updated_at: string
  // Admin routes only.
  user_id?: string
  user_email?: string | null
}

export interface MonitorCreated extends Monitor {
  warnings: string[]
}

export type MonitorEventKind =
  | 'skipped_monthly_runs'
  | 'skipped_check_in_progress'
  | 'skipped_pipelines_disabled'
  | 'waiting_for_slot'
  | 'gave_up_waiting'
  | 'paused_itself'
  // ADR-017: a point event, written closed; detail = {run_id, deadline, run_url}.
  | 'approval_expired'

export interface MonitorEvent {
  id: string
  kind: MonitorEventKind
  first_at: string
  last_at: string
  count: number
  ended_at: string | null
  detail: Record<string, unknown> | null
  // The notice this situation sent to the monitor's destination; null = none sent.
  notice: MonitorNotice | null
}

export interface MonitorNotice {
  kind: 'webhook' | 'email'
  destination_id: string | null
  status: 'pending' | 'delivered' | 'exhausted'
  attempts: number
  last_error: string | null
  next_attempt_at: string
  delivered_at: string | null
}

export interface MonitorHistoryItem {
  type: 'check' | 'event'
  at: string
  check: PipelineRunSummary | null
  event: MonitorEvent | null
}

export interface AdminMonitorDetail extends Monitor {
  history: MonitorHistoryItem[]
}

// ---------------------------------------------------------------------------
// Destinations (PRD-017 · ADR-015 §2)
// ---------------------------------------------------------------------------

export type DestinationKind = 'bucket' | 'postgres' | 'smtp' | 'webhook'

// Never the secret; a webhook's URL is a credential, so it shows only display_host.
export interface Destination {
  id: string
  name: string
  kind: DestinationKind
  address: Record<string, unknown>
  display_host: string
  created_at: string
  updated_at: string
  // Admin routes only.
  user_id?: string
  user_email?: string
}

export interface DestinationCheck {
  check: string
  outcome: 'passed' | 'failed' | 'warning'
  message: string | null
}

// A warning (a bucket that refused the delete) still passes.
export interface DestinationTestResult {
  passed: boolean
  checks: DestinationCheck[]
}

export interface DestinationSaved extends Destination {
  // Null: an edit that changed neither address nor secret runs no test.
  test: DestinationTestResult | null
}

// 422 from a save whose test failed.
export interface DestinationTestFailed {
  error: 'destination_test_failed'
  message: string
  checks: DestinationCheck[]
}

// 409 from a delete: what still uses it.
export interface DestinationInUse {
  error: 'destination_in_use'
  message: string
  pipelines: Array<{ id: string; name: string }>
  runs: Array<{ id: string; pipeline_id: string }>
  monitors: Array<{ id: string; name: string }>
}

// ---------------------------------------------------------------------------
// Block catalog (PRD-016 R4, PRD-017 R2/R6)
// ---------------------------------------------------------------------------

export interface CatalogTiming {
  attempt_seconds: number
  queue_wait_seconds: number
  attempts: number
  first_wait_seconds: number
  backoff: number
  max_wait_seconds: number
  budget_seconds: number
  need_seconds: number
}

export interface CatalogBlock {
  type: string
  kind: 'content' | 'effect'
  config_schema_version: number
  consumes: string[]
  // The kinds of saved destination its destination_id may name; empty if it takes none.
  destination_kinds: DestinationKind[]
  sink: { reversible: boolean; undo: CatalogTiming | null } | null
  timings: Record<string, CatalogTiming>
}

export interface PipelineCatalog {
  blocks: CatalogBlock[]
  run_limit_seconds: number
}
