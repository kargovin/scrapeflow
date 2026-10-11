import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import Editor from '@monaco-editor/react'
import '../lib/monaco'
import { type Pipeline } from '../api'
import { useApi } from '../lib/useApi'
import ErrorBox from '../components/ErrorBox'

// Today's job recipe as a pipeline (PRD-016 R6), with the URL bound to a run input.
const TEMPLATE = {
  inputs: { url: { type: 'url' } },
  blocks: [
    { id: 'fetch', type: 'scrape', config: { url: { $input: 'url' }, output_format: 'html' } },
    { id: 'clean', type: 'clean', input: 'fetch', config: {} },
    {
      id: 'guard',
      type: 'validate',
      input: 'clean',
      config: { rules: [{ kind: 'min_length', chars: 200 }] },
    },
    {
      id: 'notify',
      type: 'webhook',
      input: 'guard',
      config: { url: 'https://example.com/hooks/scrapeflow' },
    },
  ],
}

const HELP = [
  'Block types: scrape, clean, llm_extract, validate, webhook — each block names the block it reads from in "input".',
  'Secret fields (scrape proxy_url / cookies, webhook secret) are write-only: set them in config; they are stored encrypted and listed by name in "secrets".',
  'Leaving a secret field out of a kept block keeps its stored value; null clears it.',
]

export default function PipelineEditor() {
  const { pipelineId } = useParams<{ pipelineId: string }>()
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isNew = !pipelineId
  const [name, setName] = useState('')
  const [text, setText] = useState(isNew ? JSON.stringify(TEMPLATE, null, 2) : '')
  const [parseError, setParseError] = useState<string | null>(null)

  const { data: pipeline } = useQuery<Pipeline>({
    queryKey: ['pipeline', 'user', pipelineId],
    queryFn: () => api.get(`/pipelines/${pipelineId}`),
    enabled: !isNew,
  })

  useEffect(() => {
    if (pipeline) {
      setName(pipeline.name)
      setText(JSON.stringify(pipeline.definition, null, 2))
    }
  }, [pipeline])

  const save = useMutation({
    mutationFn: (definition: unknown) =>
      isNew
        ? api.post<Pipeline>('/pipelines', { name, definition })
        : api.patch<Pipeline>(`/pipelines/${pipelineId}`, {
            name: name !== pipeline?.name ? name : undefined,
            definition,
          }),
    onSuccess: saved => {
      qc.invalidateQueries({ queryKey: ['pipelines'] })
      qc.invalidateQueries({ queryKey: ['pipeline', 'user', saved.id] })
      navigate(`/app/dashboard/pipelines/${saved.id}`)
    },
  })

  const submit = () => {
    setParseError(null)
    let definition: unknown
    try {
      definition = JSON.parse(text)
    } catch (e) {
      setParseError(`The definition is not valid JSON: ${String(e)}`)
      return
    }
    save.mutate(definition)
  }

  if (!isNew && !pipeline) return <p className="text-sm text-gray-500">Loading…</p>

  return (
    <div>
      <button
        onClick={() => navigate(isNew ? '/app/dashboard/pipelines' : `/app/dashboard/pipelines/${pipelineId}`)}
        className="mb-4 text-sm text-indigo-600 hover:underline"
      >
        ← Back
      </button>
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        {isNew ? 'New pipeline' : `Edit ${pipeline?.name} (saves v${(pipeline?.version ?? 0) + 1})`}
      </h2>

      <label className="block text-xs font-medium text-gray-500 uppercase mb-1">Name</label>
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        maxLength={200}
        className="w-full max-w-md mb-4 text-sm border border-gray-300 rounded px-2 py-1.5"
      />

      <label className="block text-xs font-medium text-gray-500 uppercase mb-1">Definition</label>
      <div className="border border-gray-200 rounded overflow-hidden mb-2">
        <Editor
          height="460px"
          language="json"
          value={text}
          onChange={v => setText(v ?? '')}
          options={{ minimap: { enabled: false }, fontSize: 12, tabSize: 2 }}
        />
      </div>
      <ul className="text-xs text-gray-500 space-y-0.5 mb-4">
        {HELP.map(h => <li key={h}>{h}</li>)}
      </ul>

      <button
        onClick={submit}
        disabled={!name.trim() || save.isPending}
        className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
      >
        {save.isPending ? 'Saving…' : 'Save'}
      </button>
      {parseError && <ErrorBox error={parseError} />}
      <ErrorBox error={save.error} />
    </div>
  )
}
