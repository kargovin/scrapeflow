import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import Editor from '@monaco-editor/react'
import '../lib/monaco'
import { type Destination, type Pipeline, type PipelineCatalog } from '../api'
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
  'Block types: scrape, clean, llm_extract, validate, approval, webhook, bucket, postgres, email — each block names the block it reads from in "input".',
  'An approval block makes the run wait for your answer before the steps after it: {"deadline_hours": 24, "destination_id": "<id>"}. The request also goes to the destination, a saved webhook or email destination, if you name one (leave it out: this site only). Not the first or last block; one per pipeline.',
  'A bucket block writes its input to a saved bucket destination: {"destination_id": "<id>"}. A webhook block takes either "url" (+ "secret") or "destination_id" of a saved webhook destination.',
  'A postgres block writes its input — JSON only: an extraction, or a page scraped as JSON — as one row of a saved postgres destination\'s table: {"destination_id": "<id>"}. The table needs the documented columns and a unique key on (run_id, block_id).',
  'An email block sends its input — the page or extraction, inline up to 256 KiB — to a saved email destination\'s recipients: {"destination_id": "<id>"}. The subject names the pipeline and the run; on a monitor\'s check the message also says what changed. Like a webhook, "events" picks when it sends: ["pipeline.result"] (default), ["pipeline.failed"] (only when the run breaks) or both.',
  'Put every bucket and postgres block before the first webhook or email: if a later step fails, ScrapeFlow undoes what they received — but not once a webhook or email has gone out.',
  'Secret fields (scrape proxy_url / cookies, webhook secret) are write-only: set them in config; they are stored encrypted and listed by name in "secrets".',
  'Leaving a secret field out of a kept block keeps its stored value; null clears it.',
]

function hours(seconds: number): string {
  return seconds < 3600 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} h`
}

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

  const { data: destinations } = useQuery<Destination[]>({
    queryKey: ['destinations', 'user', 'me'],
    queryFn: () => api.get('/destinations'),
  })
  // PRD-017 R6: each block's time budget, so a long chain can be added up before saving.
  const { data: catalog } = useQuery<PipelineCatalog>({
    queryKey: ['pipeline-catalog'],
    queryFn: () => api.get('/pipelines/catalog'),
    staleTime: Infinity,
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

      <details className="mb-2 text-xs text-gray-600">
        <summary className="cursor-pointer text-gray-700">Your destinations ({destinations?.length ?? 0})</summary>
        {!destinations?.length ? (
          <p className="mt-1">None saved yet — add one under Destinations.</p>
        ) : (
          <table className="mt-1 text-xs">
            <tbody>
              {destinations.map(d => (
                <tr key={d.id}>
                  <td className="pr-3 py-0.5 text-gray-900">{d.name}</td>
                  <td className="pr-3 text-gray-500">{d.kind}</td>
                  <td className="pr-3 text-gray-500">{d.display_host}</td>
                  <td className="pr-3 font-mono select-all">{d.id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>
      {catalog && (
        <details className="mb-4 text-xs text-gray-600">
          <summary className="cursor-pointer text-gray-700">
            Block budgets (a run may take {hours(catalog.run_limit_seconds)})
          </summary>
          <table className="mt-1 text-xs">
            <tbody>
              {catalog.blocks.map(b => (
                <tr key={b.type} className="align-top">
                  <td className="pr-3 py-0.5 text-gray-900">{b.type}</td>
                  <td className="pr-3 text-gray-500">
                    {b.sink ? (b.sink.reversible ? 'undoable sink' : 'cannot be undone') : b.kind}
                    {b.destination_kinds.length > 0 && ` → ${b.destination_kinds.join(' or ')} destination`}
                  </td>
                  <td className="pr-3 text-gray-500">
                    {Object.entries(b.timings)
                      .map(([label, t]) => `${label}: up to ${hours(t.need_seconds)} (${t.attempts} tries)`)
                      .join('; ')}
                    {b.sink?.undo && `; undo reserves ${hours(b.sink.undo.need_seconds)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

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
