import { useState } from 'react'
import { useMutation, useQuery, useQueryClient, type Query } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { type Pipeline, type PipelineRun, type PipelineRunSummary } from '../api'
import { useApi } from '../lib/useApi'
import ErrorBox from '../components/ErrorBox'
import StatusPill, { runStatus } from '../components/StatusPill'

export default function PipelineDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { pipelineId } = useParams<{ pipelineId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/pipelines' : '/pipelines'}/${pipelineId}`
  const routeBase = isAdmin ? '/app/admin/pipelines' : '/app/dashboard/pipelines'
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [version, setVersion] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { data: pipeline, isLoading, isError } = useQuery<Pipeline>({
    queryKey: ['pipeline', mode, pipelineId],
    queryFn: () => api.get(apiBase),
  })

  const { data: runs } = useQuery<PipelineRunSummary[]>({
    queryKey: ['pipeline-runs', mode, pipelineId],
    queryFn: () => api.get(`${apiBase}/runs?limit=50`),
    refetchInterval: (query: Query<PipelineRunSummary[]>) =>
      (query.state.data ?? []).some(r => r.status === 'running') ? 5_000 : false,
  })

  const run = useMutation({
    mutationFn: () =>
      api.post<PipelineRun>(`/pipelines/${pipelineId}/runs`, {
        inputs,
        version: version ? Number(version) : undefined,
      }),
    onSuccess: created => {
      qc.invalidateQueries({ queryKey: ['pipeline-runs', mode, pipelineId] })
      navigate(`${routeBase}/${pipelineId}/runs/${created.id}`)
    },
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/pipelines/${pipelineId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pipelines'] })
      navigate(routeBase)
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (isError || !pipeline) return <p className="text-sm text-red-600">Pipeline not found.</p>

  const inputNames = Object.keys(pipeline.definition.inputs ?? {})

  return (
    <div>
      <button onClick={() => navigate(routeBase)} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back to pipelines
      </button>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">
          {pipeline.name} <span className="text-sm font-normal text-gray-400">v{pipeline.version}</span>
          {pipeline.deleted_at && <span className="ml-2 text-xs text-gray-400">(deleted)</span>}
        </h2>
        {!isAdmin && (
          <button
            onClick={() => navigate(`${routeBase}/${pipelineId}/edit`)}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
          >
            Edit
          </button>
        )}
      </div>

      {isAdmin && (
        <p className="mb-4 text-sm text-gray-500">Owner: {pipeline.user_email ?? pipeline.user_id}</p>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-6">
        {pipeline.definition.blocks.map((b, i) => (
          <span key={b.id} className="flex items-center gap-2">
            {i > 0 && <span className="text-gray-400">→</span>}
            <span className="px-2 py-1 bg-white border border-gray-200 rounded text-xs">
              <span className="font-semibold text-gray-800">{b.type}</span>
              <span className="ml-1 text-gray-400">{b.id}</span>
              {b.secrets.length > 0 && (
                <span className="ml-1 text-gray-400" title={`secrets set: ${b.secrets.join(', ')}`}>🔒</span>
              )}
            </span>
          </span>
        ))}
      </div>

      {!isAdmin && (
        <div className="bg-white border border-gray-200 rounded-lg p-4 mb-6">
          <h3 className="text-sm font-semibold text-gray-900 mb-3">Run</h3>
          <div className="flex flex-wrap items-end gap-3">
            {inputNames.map(n => (
              <label key={n} className="text-xs text-gray-500">
                {n}
                <input
                  value={inputs[n] ?? ''}
                  onChange={e => setInputs({ ...inputs, [n]: e.target.value })}
                  className="block mt-1 w-72 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
                />
              </label>
            ))}
            <label className="text-xs text-gray-500">
              version
              <input
                value={version}
                onChange={e => setVersion(e.target.value.replace(/\D/g, ''))}
                placeholder={`latest (v${pipeline.version})`}
                className="block mt-1 w-32 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
              />
            </label>
            <button
              onClick={() => run.mutate()}
              disabled={run.isPending || inputNames.some(n => !inputs[n])}
              className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
            >
              {run.isPending ? 'Starting…' : 'Run'}
            </button>
          </div>
          <ErrorBox error={run.error} />
        </div>
      )}

      <h3 className="text-sm font-semibold text-gray-900 mb-2">Runs</h3>
      {!runs?.length ? (
        <p className="text-sm text-gray-500 mb-6">No runs yet.</p>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                {['Status', 'Version', 'Started', 'Finished', 'Error'].map(h => (
                  <th key={h} className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {runs.map(r => (
                <tr
                  key={r.id}
                  onClick={() => navigate(`${routeBase}/${pipelineId}/runs/${r.id}`)}
                  className="hover:bg-gray-50 cursor-pointer"
                >
                  <td className="px-4 py-3"><StatusPill status={runStatus(r)} /></td>
                  <td className="px-4 py-3 text-gray-500">v{r.version}</td>
                  <td className="px-4 py-3 text-gray-500">{new Date(r.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {r.finished_at ? new Date(r.finished_at).toLocaleString() : '—'}
                  </td>
                  <td className="px-4 py-3 text-gray-500 max-w-xs truncate" title={r.error ?? ''}>
                    {r.error ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <details className="mb-6">
        <summary className="text-sm text-gray-600 cursor-pointer">Definition (JSON)</summary>
        <pre className="mt-2 p-3 bg-white border border-gray-200 rounded text-xs overflow-auto max-h-96">
          {JSON.stringify(pipeline.definition, null, 2)}
        </pre>
      </details>

      {!isAdmin && (
        <div className="border border-red-200 rounded-lg p-4 bg-red-50">
          <h3 className="text-sm font-semibold text-red-800 mb-1">Delete pipeline</h3>
          <p className="text-xs text-red-600 mb-3">
            A pipeline with runs is kept for its runs and their results; its name stays taken.
          </p>
          {!confirmDelete ? (
            <button
              onClick={() => setConfirmDelete(true)}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700"
            >
              Delete
            </button>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={() => remove.mutate()}
                disabled={remove.isPending}
                className="px-3 py-1.5 text-sm bg-red-700 text-white rounded hover:bg-red-800 disabled:opacity-50"
              >
                {remove.isPending ? 'Deleting…' : 'Confirm delete'}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
              >
                Keep
              </button>
            </div>
          )}
          <ErrorBox error={remove.error} />
        </div>
      )}
    </div>
  )
}
