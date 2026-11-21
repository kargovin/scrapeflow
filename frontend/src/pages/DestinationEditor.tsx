import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type DestinationKind, type DestinationSaved, type DestinationTestResult } from '../api'
import { useApi } from '../lib/useApi'
import { KINDS, KIND_LABELS, initialValues, newForm, type Values } from '../lib/destinationForm'
import DestinationChecks, { failedChecks } from '../components/DestinationChecks'
import DestinationFields from '../components/DestinationFields'
import ErrorBox from '../components/ErrorBox'

// R5: tested before it is saved, so a delete warning is seen first. Saving tests it again.
export default function DestinationEditor() {
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<DestinationKind>('bucket')
  const [values, setValues] = useState<Values>(() => initialValues('bucket'))
  const [tested, setTested] = useState<{ form: string; result: DestinationTestResult } | null>(null)

  const form = newForm(kind, values)
  const formKey = JSON.stringify(form)
  const current = tested?.form === formKey ? tested.result : null

  const test = useMutation({
    mutationFn: () => api.post<DestinationTestResult>('/destinations/test', form),
    onSuccess: result => setTested({ form: formKey, result }),
  })
  const save = useMutation({
    mutationFn: () => api.post<DestinationSaved>('/destinations', { ...form, name: name.trim() }),
    onSuccess: saved => {
      qc.invalidateQueries({ queryKey: ['destinations'] })
      navigate(`/app/dashboard/destinations/${saved.id}`)
    },
  })
  const refused = failedChecks(save.error)
  const warned = current?.checks.some(c => c.outcome === 'warning')

  return (
    <div className="max-w-3xl">
      <button onClick={() => navigate('/app/dashboard/destinations')} className="mb-4 text-sm text-indigo-600 hover:underline">
        ← Back
      </button>
      <h2 className="text-lg font-semibold text-gray-900 mb-4">New destination</h2>

      <div className="flex flex-wrap gap-4 mb-4">
        <label className="text-xs text-gray-600">
          Name
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            maxLength={100}
            className="block mt-1 w-64 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
          />
        </label>
        <label className="text-xs text-gray-600">
          Kind
          <select
            value={kind}
            onChange={e => {
              const k = e.target.value as DestinationKind
              setKind(k)
              setValues(initialValues(k))
            }}
            className="block mt-1 text-sm border border-gray-300 rounded px-2 py-1 text-gray-900"
          >
            {KINDS.map(k => (
              <option key={k} value={k}>{KIND_LABELS[k]}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="bg-white border border-gray-200 rounded-lg p-4 mb-4">
        <DestinationFields kind={kind} values={values} onChange={setValues} />
      </div>
      <p className="text-xs text-gray-500 mb-3">
        Every address must be public: private, local and loopback addresses are refused. The test
        connects from ScrapeFlow{kind === 'bucket' ? ', writes one empty object and deletes it' : ''}
        {kind === 'smtp' ? ' and logs in; it sends no mail' : ''}
        {kind === 'webhook' ? '; it sends no request' : ''}.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => test.mutate()}
          disabled={test.isPending}
          className="px-3 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
        >
          {test.isPending ? 'Testing…' : 'Test'}
        </button>
        <button
          onClick={() => save.mutate()}
          disabled={!name.trim() || !current?.passed || save.isPending}
          title={current?.passed ? undefined : 'Test the destination first'}
          className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
        >
          {save.isPending ? 'Saving…' : warned ? 'Save with this warning' : 'Save'}
        </button>
        {tested && !current && <span className="text-xs text-gray-500">The form changed since the test — test it again.</span>}
      </div>
      <ErrorBox error={test.error} subject="form" />
      {current && <DestinationChecks result={current} />}
      {refused ? <DestinationChecks result={refused} /> : <ErrorBox error={save.error} subject="form" />}
    </div>
  )
}
