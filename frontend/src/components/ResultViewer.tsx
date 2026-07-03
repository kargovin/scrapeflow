import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { apiGet, type JobResult } from '../api'
import '../lib/monaco' // side-effect: point Monaco at locally-bundled assets
import './ResultViewer.css'

const LANGUAGE_BY_FORMAT: Record<string, string> = {
  json: 'json',
  markdown: 'markdown',
  html: 'html',
}

const EXT_BY_FORMAT: Record<string, string> = {
  json: 'json',
  markdown: 'md',
  html: 'html',
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw // malformed JSON — show as stored rather than throwing
  }
}

interface Props {
  jobId: string
  outputFormat: string
  status: string
  resultPath: string | null
  token: string
}

export default function ResultViewer({ jobId, outputFormat, status, resultPath, token }: Props) {
  const [tab, setTab] = useState<'source' | 'preview'>('source')
  const [copied, setCopied] = useState(false)

  const enabled = status === 'completed' && !!resultPath && !!token
  const { data, isLoading, isError, error } = useQuery<JobResult>({
    queryKey: ['admin-job-result', jobId],
    queryFn: () => apiGet(`/admin/jobs/${jobId}/result`, token),
    enabled,
  })

  const format = data?.output_format ?? outputFormat
  const hasPreview = format === 'markdown' || format === 'html'
  const source = useMemo(() => {
    if (!data) return ''
    return format === 'json' ? prettyJson(data.content) : data.content
  }, [data, format])

  const copy = async () => {
    await navigator.clipboard.writeText(source)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const download = () => {
    const ext = EXT_BY_FORMAT[format] ?? 'txt'
    const blob = new Blob([source], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${jobId}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-gray-900">Result</h3>
          <span className="inline-flex px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-600">
            {format}
          </span>
        </div>
        {enabled && data && (
          <div className="flex items-center gap-2">
            {hasPreview && (
              <div className="flex rounded border border-gray-300 overflow-hidden text-xs">
                <button
                  onClick={() => setTab('source')}
                  className={`px-2 py-1 ${tab === 'source' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600'}`}
                >
                  Source
                </button>
                <button
                  onClick={() => setTab('preview')}
                  className={`px-2 py-1 ${tab === 'preview' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600'}`}
                >
                  Preview
                </button>
              </div>
            )}
            <button
              onClick={copy}
              className="px-2 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50"
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button
              onClick={download}
              className="px-2 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50"
            >
              Download
            </button>
          </div>
        )}
      </div>

      <div>
        {!enabled ? (
          <p className="px-4 py-6 text-sm text-gray-500">
            No result available for this status.
          </p>
        ) : isLoading ? (
          <p className="px-4 py-6 text-sm text-gray-500">Loading result…</p>
        ) : isError ? (
          <p className="px-4 py-6 text-sm text-red-600">
            Failed to load result: {String(error)}
          </p>
        ) : !data ? null : tab === 'preview' && format === 'html' ? (
          <iframe
            // sandbox="" blocks scripts/forms — safe to render arbitrary scraped
            // HTML. Remote images/styles may still load.
            sandbox=""
            srcDoc={data.content}
            title="HTML preview"
            className="w-full border-0 bg-white"
            style={{ height: 480 }}
          />
        ) : tab === 'preview' && format === 'markdown' ? (
          <div className="md-preview px-4 py-4 overflow-auto" style={{ maxHeight: 480 }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.content}</ReactMarkdown>
          </div>
        ) : (
          <Editor
            height="480px"
            language={LANGUAGE_BY_FORMAT[format] ?? 'plaintext'}
            value={source}
            theme="light"
            loading={<p className="px-4 py-6 text-sm text-gray-500">Loading editor…</p>}
            options={{
              readOnly: true,
              lineNumbers: 'on',
              minimap: { enabled: false },
              folding: true,
              wordWrap: 'on',
              scrollBeyondLastLine: false,
              fontSize: 13,
            }}
          />
        )}
      </div>

      {data?.warnings && data.warnings.length > 0 && (
        <div className="border-t border-gray-100 px-4 py-2 bg-amber-50">
          <p className="text-xs font-medium text-amber-800 mb-1">Warnings</p>
          <ul className="list-disc pl-5 text-xs text-amber-700">
            {data.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
