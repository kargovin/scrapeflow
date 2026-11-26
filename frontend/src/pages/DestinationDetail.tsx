import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  type Destination,
  type DestinationInUse,
  type DestinationSaved,
  type DestinationTestResult,
} from '../api'
import { useApi } from '../lib/useApi'
import { KIND_LABELS, addressRows, editForm, initialValues, type Values } from '../lib/destinationForm'
import DestinationChecks, { failedChecks } from '../components/DestinationChecks'
import DestinationFields from '../components/DestinationFields'
import ErrorBox from '../components/ErrorBox'
import { LoadFailed, RefreshFailed } from '../components/LoadError'

function inUse(error: unknown): DestinationInUse | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const detail = error.detail as Partial<DestinationInUse> | null
  return detail?.error === 'destination_in_use' ? (detail as DestinationInUse) : null
}

export default function DestinationDetail({ mode }: { mode: 'admin' | 'user' }) {
  const { destinationId } = useParams<{ destinationId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isAdmin = mode === 'admin'
  const apiBase = `${isAdmin ? '/admin/destinations' : '/destinations'}/${destinationId}`
  const routeBase = isAdmin ? '/app/admin/destinations' : '/app/dashboard/destinations'
  const key = ['destination', mode, destinationId]
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [values, setValues] = useState<Values>({})
  const [tested, setTested] = useState<{ form: string; result: DestinationTestResult } | null>(null)
  const [saved, setSaved] = useState<DestinationTestResult | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const { data: destination, isLoading, error, refetch } = useQuery<Destination>({
    queryKey: key,
    queryFn: () => api.get(apiBase),
  })

  const edit = destination ? editForm(destination.kind, values, destination) : null
  const changesTarget = !!edit && (edit.address !== undefined || edit.secret !== undefined)
  const editKey = JSON.stringify(edit)
  const editTest = tested?.form === editKey ? tested.result : null

  const testStored = useMutation({
    mutationFn: () => api.post<DestinationTestResult>(`/destinations/${destinationId}/test`, {}),
  })
  const testEdit = useMutation({
    mutationFn: () => api.post<DestinationTestResult>(`/destinations/${destinationId}/test`, edit),
    onSuccess: result => setTested({ form: editKey, result }),
  })
  const save = useMutation({
    mutationFn: () =>
      api.patch<DestinationSaved>(`/destinations/${destinationId}`, {
        name: name.trim() !== destination?.name ? name.trim() : undefined,
        ...edit,
      }),
    onSuccess: result => {
      setEditing(false)
      setSaved(result.test)
      qc.setQueryData(key, result)
      qc.invalidateQueries({ queryKey: ['destinations'] })
    },
  })
  const remove = useMutation({
    mutationFn: () => api.del(`/destinations/${destinationId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['destinations'] })
      navigate(routeBase)
    },
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (!destination) return <LoadFailed error={error} subject="Destination" onRetry={refetch} />

  const rows: [string, string][] = [
    ...(isAdmin ? [['Owner', destination.user_email ?? destination.user_id ?? '—'] as [string, string]] : []),
    ['Kind', KIND_LABELS[destination.kind]],
    ['Host', destination.display_host],
    ...addressRows(destination),
    ...(destination.kind === 'webhook'
      ? [['URL', 'hidden — a webhook URL is a credential; paste a new one to change it'] as [string, string]]
      : []),
    ['Credentials', 'stored encrypted — never shown'],
    ['Created', new Date(destination.created_at).toLocaleString()],
    ['Updated', new Date(destination.updated_at).toLocaleString()],
  ]
  const blocked = inUse(remove.error)
  const refused = failedChecks(save.error)
  const pipelinesBase = isAdmin ? '/app/admin/pipelines' : '/app/dashboard/pipelines'
  const warned = editTest?.checks.some(c => c.outcome === 'warning')

  return (
    <div className="max-w-4xl">
      <RefreshFailed error={error} onRetry={refetch} />
      <button onClick={() => navigate(routeBase)} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back to destinations
      </button>
      <h2 className="text-lg font-semibold text-gray-900 mb-4">{destination.name}</h2>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-4">
        <dl className="divide-y divide-gray-100">
          {rows.map(([label, value]) => (
            <div key={label} className="px-4 py-2 sm:grid sm:grid-cols-4 sm:gap-4">
              <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
              <dd className="text-sm text-gray-900 sm:col-span-3 break-all">{value}</dd>
            </div>
          ))}
        </dl>
      </div>

      {saved && (
        <div className="mb-4">
          <p className="text-xs text-gray-500">Saved. The test it passed:</p>
          <DestinationChecks result={saved} />
        </div>
      )}

      {!isAdmin && (
        <>
          <div className="flex flex-wrap gap-2 mb-2">
            <button
              onClick={() => testStored.mutate()}
              disabled={testStored.isPending}
              className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
            >
              {testStored.isPending ? 'Testing…' : 'Test'}
            </button>
            <button
              onClick={() => {
                setName(destination.name)
                setValues(initialValues(destination.kind, destination))
                setTested(null)
                setSaved(null)
                save.reset()
                setEditing(!editing)
              }}
              className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
            >
              {editing ? 'Close editing' : 'Edit'}
            </button>
          </div>
          <ErrorBox error={testStored.error} subject="destination" />
          {testStored.data && <DestinationChecks result={testStored.data} />}

          {editing && (
            <div className="bg-white border border-gray-200 rounded-lg p-4 my-4">
              <label className="block text-xs text-gray-600 mb-4">
                Name
                <input
                  value={name}
                  onChange={e => setName(e.target.value)}
                  maxLength={100}
                  className="block mt-1 w-64 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
                />
              </label>
              <DestinationFields kind={destination.kind} values={values} onChange={setValues} editing />
              <p className="mt-3 text-xs text-gray-500">
                Runs use the new address and credentials from their next attempt. A delivery already
                made is undone where it was written, whatever this says now.
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {changesTarget && (
                  <button
                    onClick={() => testEdit.mutate()}
                    disabled={testEdit.isPending}
                    className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
                  >
                    {testEdit.isPending ? 'Testing…' : 'Test the changes'}
                  </button>
                )}
                <button
                  onClick={() => save.mutate()}
                  disabled={!name.trim() || save.isPending || (changesTarget && !editTest?.passed)}
                  title={changesTarget && !editTest?.passed ? 'Test the changes first' : undefined}
                  className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
                >
                  {save.isPending ? 'Saving…' : changesTarget && warned ? 'Save with this warning' : 'Save'}
                </button>
                {tested && changesTarget && !editTest && (
                  <span className="text-xs text-gray-500">The form changed since the test — test it again.</span>
                )}
              </div>
              <ErrorBox error={testEdit.error} subject="form" />
              {changesTarget && editTest && <DestinationChecks result={editTest} />}
              {refused ? <DestinationChecks result={refused} /> : <ErrorBox error={save.error} subject="form" />}
            </div>
          )}

          <div className="mt-6 border border-red-200 rounded-lg p-4 bg-red-50">
            <h3 className="text-sm font-semibold text-red-800 mb-1">Delete destination</h3>
            <p className="text-xs text-red-600 mb-3">
              Refused while a pipeline, a running run or a monitor uses it. What it already received
              stays where it is.
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
            {blocked ? (
              <div className="mt-3 border border-red-200 bg-white rounded p-3 text-xs text-red-700 space-y-2">
                <p>{blocked.message}</p>
                {blocked.pipelines.length > 0 && (
                  <p>
                    Pipelines:{' '}
                    {blocked.pipelines.map((p, i) => (
                      <span key={p.id}>
                        {i > 0 && ', '}
                        <button onClick={() => navigate(`${pipelinesBase}/${p.id}`)} className="text-indigo-600 hover:underline">
                          {p.name}
                        </button>
                      </span>
                    ))}
                  </p>
                )}
                {blocked.runs.length > 0 && (
                  <p>
                    Running runs:{' '}
                    {blocked.runs.map((r, i) => (
                      <span key={r.id}>
                        {i > 0 && ', '}
                        <button
                          onClick={() => navigate(`${pipelinesBase}/${r.pipeline_id}/runs/${r.id}`)}
                          className="text-indigo-600 hover:underline"
                        >
                          {r.id.slice(0, 8)}
                        </button>
                      </span>
                    ))}
                  </p>
                )}
                {blocked.monitors.length > 0 && (
                  <p>
                    Monitors sending notices here:{' '}
                    {blocked.monitors.map((m, i) => (
                      <span key={m.id}>
                        {i > 0 && ', '}
                        <button
                          onClick={() => navigate(`/app/dashboard/monitors/${m.id}`)}
                          className="text-indigo-600 hover:underline"
                        >
                          {m.name}
                        </button>
                      </span>
                    ))}
                  </p>
                )}
              </div>
            ) : (
              <ErrorBox error={remove.error} subject="destination" />
            )}
          </div>
        </>
      )}
    </div>
  )
}
