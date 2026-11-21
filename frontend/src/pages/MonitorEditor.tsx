import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import Editor from '@monaco-editor/react'
import '../lib/monaco'
import { type MonitorCreated, type Pipeline } from '../api'
import { useApi } from '../lib/useApi'
import ErrorBox from '../components/ErrorBox'

const HELP = [
  'A monitor runs one pinned version of a pipeline on a schedule (UTC cron, at most every 5 minutes). Its pipeline, version and inputs are fixed for its life; only the name, the schedule and the notice destination can change later.',
  'Notices (checks skipped, paused itself) always show on the monitor page; set "notice_destination_id" to the id of one of your webhook or email destinations to also be told there.',
  'Every run input goes in "inputs" (a fixed value) or in "carried": {input, from_block, path, start} takes the value at a JSON Pointer path of a block\'s output from the last check, starting at "start".',
  'A pipeline with an LLM block needs a change gate before it: a validate block with rules [{"kind": "changed"}] and when_unmet "stop", so an unchanged page is never sent to the LLM.',
]

function template(pipeline: Pipeline | undefined, pipelineId: string | null) {
  const inputs = Object.fromEntries(
    Object.entries(pipeline?.definition.inputs ?? {}).map(([name, d]) => [name, d.type === 'number' ? 0 : '']),
  )
  return {
    name: pipeline ? `${pipeline.name} monitor` : '',
    pipeline_id: pipelineId ?? '<pipeline id>',
    version: pipeline?.version ?? null,
    inputs,
    carried: [],
    schedule_cron: '0 * * * *',
    notice_destination_id: null,
  }
}

export default function MonitorEditor() {
  const [params] = useSearchParams()
  const pipelineId = params.get('pipeline')
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [parseError, setParseError] = useState<string | null>(null)

  const { data: pipeline, isLoading } = useQuery<Pipeline>({
    queryKey: ['pipeline', 'user', pipelineId],
    queryFn: () => api.get(`/pipelines/${pipelineId}`),
    enabled: !!pipelineId,
  })

  useEffect(() => {
    if (!pipelineId || pipeline) setText(JSON.stringify(template(pipeline, pipelineId), null, 2))
  }, [pipeline, pipelineId])

  const save = useMutation({
    mutationFn: (body: unknown) => api.post<MonitorCreated>('/monitors', body),
    onSuccess: created => {
      qc.invalidateQueries({ queryKey: ['monitors'] })
      qc.invalidateQueries({ queryKey: ['scheduled-jobs-usage'] })
      navigate(`/app/dashboard/monitors/${created.id}`, { state: { warnings: created.warnings } })
    },
  })

  const submit = () => {
    setParseError(null)
    let body: unknown
    try {
      body = JSON.parse(text)
    } catch (e) {
      setParseError(`The monitor is not valid JSON: ${String(e)}`)
      return
    }
    save.mutate(body)
  }

  if (pipelineId && isLoading) return <p className="text-sm text-gray-500">Loading…</p>

  return (
    <div>
      <button
        onClick={() => navigate(pipelineId ? `/app/dashboard/pipelines/${pipelineId}` : '/app/dashboard/monitors')}
        className="mb-4 text-sm text-indigo-600 hover:underline"
      >
        ← Back
      </button>
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        New monitor{pipeline ? ` of ${pipeline.name}` : ''}
      </h2>
      <div className="border border-gray-200 rounded overflow-hidden mb-2">
        <Editor
          height="360px"
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
        disabled={save.isPending}
        className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
      >
        {save.isPending ? 'Creating…' : 'Create'}
      </button>
      {parseError && <ErrorBox error={parseError} />}
      <ErrorBox error={save.error} />
    </div>
  )
}
