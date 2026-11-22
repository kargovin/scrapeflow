import { useMutation } from '@tanstack/react-query'
import { type Approval, type PipelineRun, type PipelineRunBlock } from '../api'
import { useApi } from '../lib/useApi'
import {
  approvalState,
  approvalStateInWords,
  changeSummaryInWords,
  deadlineInWords,
  locationInWords,
} from '../lib/runText'
import { changeLabel } from '../lib/monitorText'
import ErrorBox from './ErrorBox'
import StatusPill from './StatusPill'

const PILL: Record<string, string> = {
  waiting: 'waiting',
  approved: 'completed',
  rejected: 'cancelled',
  expired: 'skipped',
  superseded: 'skipped',
  closed: 'skipped',
  not_requested: 'pending',
}

// PRD-018 R5: the request shows the deadline, what changed and what has already gone out.
// Only the run's owner answers; an admin sees it read-only (R9).
export default function ApprovalPanel({
  run,
  block,
  mode,
  onAnswered,
  onOpenRun,
}: {
  run: PipelineRun
  block: PipelineRunBlock
  mode: 'admin' | 'user'
  onAnswered: () => void
  onOpenRun: (runId: string) => void
}) {
  const api = useApi()
  const state = approvalState(block)
  const answer = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') =>
      api.post<Approval>(`/pipelines/${run.pipeline_id}/runs/${run.id}/approval`, { decision }),
    // A 409 carries the state that won; the refetch shows it.
    onSettled: onAnswered,
  })

  if (state === 'not_requested') return null

  const delivered = run.blocks.filter(b => b.delivery && b.position < block.position)
  const changes = changeSummaryInWords(run.change_summary)
  const answerable =
    mode === 'user' && state === 'waiting' && run.status === 'running' && !run.cancel_requested_at

  return (
    <div
      className={`border rounded-lg p-4 mb-6 text-sm ${
        state === 'waiting' ? 'border-amber-300 bg-amber-50' : 'border-gray-200 bg-white'
      }`}
    >
      <div className="flex items-center gap-2 mb-2">
        <h3 className="font-semibold text-gray-900">Approval</h3>
        <span className="text-xs text-gray-400">{block.block_id}</span>
        <StatusPill status={PILL[state] ?? state} />
        <span className="text-xs text-gray-600">
          {approvalStateInWords(state)}
          {block.answered_at && state !== 'waiting' && ` ${new Date(block.answered_at).toLocaleString()}`}
        </span>
      </div>

      <dl className="space-y-1 mb-3">
        {block.approval_deadline && (
          <div className="flex gap-2">
            <dt className="w-32 text-xs text-gray-500 uppercase">Deadline</dt>
            <dd className="text-gray-900">{deadlineInWords(block.approval_deadline)}</dd>
          </div>
        )}
        {block.started_at && (
          <div className="flex gap-2">
            <dt className="w-32 text-xs text-gray-500 uppercase">Asked</dt>
            <dd className="text-gray-900">{new Date(block.started_at).toLocaleString()}</dd>
          </div>
        )}
        <div className="flex gap-2">
          <dt className="w-32 text-xs text-gray-500 uppercase">What changed</dt>
          <dd className="text-gray-900">
            {changes.length ? (
              <>
                {changes.map(c => <div key={c}>{c}</div>)}
                <span className="text-xs text-gray-500">The diff is below.</span>
              </>
            ) : (
              (changeLabel(run) ?? 'not a monitor check — see the steps below')
            )}
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-32 text-xs text-gray-500 uppercase">Delivered</dt>
          <dd className="text-gray-900">
            {delivered.length ? (
              <>
                {delivered.map(b => (
                  <div key={b.block_id} className="font-mono text-xs break-all">
                    {b.block_id}: {locationInWords(b.delivery!)}
                  </div>
                ))}
                <span className="text-xs text-gray-500">
                  A reject keeps these; they are removed only if this run later fails.
                </span>
              </>
            ) : (
              'nothing yet'
            )}
          </dd>
        </div>
        {block.superseded_by_run_id && (
          <div className="flex gap-2">
            <dt className="w-32 text-xs text-gray-500 uppercase">Superseded by</dt>
            <dd>
              <button
                onClick={() => onOpenRun(block.superseded_by_run_id!)}
                className="text-indigo-600 hover:underline"
              >
                the check {block.superseded_by_run_id.slice(0, 8)}
              </button>
            </dd>
          </div>
        )}
      </dl>

      {state === 'waiting' && mode === 'admin' && (
        <p className="text-xs text-gray-600">Only the owner can approve or reject.</p>
      )}
      {state !== 'waiting' && block.status === 'waiting' && (
        <p className="text-xs text-gray-600">Answer recorded — the run picks it up in a moment.</p>
      )}
      {answerable && (
        <>
          <p className="text-xs text-gray-600 mb-2">
            Approve: the run carries on. Reject: the run ends completed and the steps after this one
            are skipped; nothing already delivered is undone.
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => answer.mutate('approved')}
              disabled={answer.isPending}
              className="px-3 py-1.5 text-sm bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
            >
              Approve
            </button>
            <button
              onClick={() => answer.mutate('rejected')}
              disabled={answer.isPending}
              className="px-3 py-1.5 text-sm border border-gray-300 bg-white rounded hover:bg-gray-50 disabled:opacity-50"
            >
              Reject
            </button>
          </div>
        </>
      )}
      <ErrorBox error={answer.error} subject="approval" />
    </div>
  )
}
