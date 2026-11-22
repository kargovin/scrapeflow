import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type Approval } from '../api'
import { useApi } from '../lib/useApi'
import { useWaitingApprovals } from '../lib/useApprovals'
import { changeSummaryInWords, deadlineInWords, locationInWords } from '../lib/runText'
import { changeLabel } from '../lib/monitorText'
import ErrorBox from '../components/ErrorBox'
import Pager from '../components/Pager'

// PRD-018 R9: one list across monitors and pipelines, soonest deadline first. Only the owner
// answers; an admin sees every user's, read-only.
export default function Approvals({ mode }: { mode: 'admin' | 'user' }) {
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isAdmin = mode === 'admin'
  const base = isAdmin ? '/app/admin' : '/app/dashboard'
  const [offset, setOffset] = useState(0)
  const limit = 50

  const { data: approvals, isLoading, isError } = useWaitingApprovals({ mode, limit, offset, refetchInterval: 30_000 })

  const answer = useMutation({
    mutationFn: ({ approval, decision }: { approval: Approval; decision: 'approved' | 'rejected' }) =>
      api.post<Approval>(`/pipelines/${approval.pipeline_id}/runs/${approval.run_id}/approval`, { decision }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['approvals'] }),
  })

  const runLink = (a: Approval) => `${base}/pipelines/${a.pipeline_id}/runs/${a.run_id}`

  return (
    <div>
      <h2 className="text-lg font-semibold text-gray-900 mb-1">Approvals waiting</h2>
      <p className="text-xs text-gray-500 mb-4">
        {isAdmin
          ? 'Every user’s runs waiting at an Approval step. Only the owner can approve or reject.'
          : 'Runs waiting for your answer at an Approval step. Approve: the run carries on. Reject: it ends completed, the steps after the Approval skipped; nothing already delivered is undone. No answer by the deadline: it ends expired.'}
      </p>
      <ErrorBox error={answer.error} subject="approval" />

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load approvals.</p>
      ) : !approvals?.length && offset === 0 ? (
        <p className="text-sm text-gray-500">Nothing is waiting for approval.</p>
      ) : (
        <>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mt-3">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  {['Pipeline', ...(isAdmin ? ['Owner'] : []), 'Asked', 'Deadline', 'What changed', 'Delivered', ''].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {approvals!.map(a => {
                  const changes = changeSummaryInWords(a.change_summary)
                  return (
                    <tr key={a.run_id} className="align-top">
                      <td className="px-4 py-3">
                        <button onClick={() => navigate(runLink(a))} className="text-indigo-600 hover:underline font-medium">
                          {a.pipeline_name}
                        </button>
                        <span className="ml-1 text-xs text-gray-400">{a.block_id}</span>
                        {a.monitor_id && (
                          <button
                            onClick={() => navigate(`${base}/monitors/${a.monitor_id}`)}
                            className="block text-xs text-indigo-600 hover:underline"
                          >
                            monitor check
                          </button>
                        )}
                      </td>
                      {isAdmin && <td className="px-4 py-3 text-gray-500">{a.user_email ?? a.user_id}</td>}
                      <td className="px-4 py-3 text-gray-500 whitespace-nowrap">
                        {a.requested_at ? new Date(a.requested_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-4 py-3 text-gray-900">{a.deadline ? deadlineInWords(a.deadline) : '—'}</td>
                      <td className="px-4 py-3 text-gray-700 text-xs">
                        {changes.length ? changes.map(c => <div key={c}>{c}</div>) : (changeLabel(a) ?? '—')}
                      </td>
                      <td className="px-4 py-3 text-gray-700 text-xs max-w-xs break-all">
                        {a.deliveries.length
                          ? a.deliveries.map(d => <div key={d.block_id}>{d.block_id}: {locationInWords(d)}</div>)
                          : 'nothing yet'}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        {isAdmin ? (
                          <span className="text-xs text-gray-400">read-only</span>
                        ) : (
                          <div className="flex gap-2">
                            <button
                              onClick={() => answer.mutate({ approval: a, decision: 'approved' })}
                              disabled={answer.isPending}
                              className="px-2 py-1 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => answer.mutate({ approval: a, decision: 'rejected' })}
                              disabled={answer.isPending}
                              className="px-2 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
                            >
                              Reject
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {isAdmin && <Pager offset={offset} limit={limit} count={approvals?.length ?? 0} onChange={setOffset} />}
        </>
      )}
    </div>
  )
}
