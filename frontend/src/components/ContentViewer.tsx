import { useMemo, useState, type ReactNode } from 'react'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
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
  title: string
  subtitle?: ReactNode
  format: string
  // null: nothing to show yet; `placeholder` renders instead and the actions are hidden.
  content: string | null
  placeholder?: ReactNode
  filename: string
  footer?: ReactNode
}

// One stored output: Source/Preview (markdown and HTML), Copy, Download.
export default function ContentViewer({ title, subtitle, format, content, placeholder, filename, footer }: Props) {
  const [tab, setTab] = useState<'source' | 'preview'>('source')
  const [copied, setCopied] = useState(false)

  const hasPreview = format === 'markdown' || format === 'html'
  const source = useMemo(() => {
    if (content === null) return ''
    return format === 'json' ? prettyJson(content) : content
  }, [content, format])

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
    a.download = `${filename}.${ext}`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
      <div className="flex items-center justify-between gap-2 border-b border-gray-100 px-4 py-2">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="text-sm font-semibold text-gray-900 shrink-0">{title}</h3>
          {subtitle}
        </div>
        {content !== null && (
          <div className="flex items-center gap-2 shrink-0">
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
        {content === null ? (
          placeholder
        ) : tab === 'preview' && format === 'html' ? (
          <iframe
            // sandbox="" blocks scripts/forms — safe to render arbitrary scraped
            // HTML. Remote images/styles may still load.
            sandbox=""
            srcDoc={content}
            title="HTML preview"
            className="w-full border-0 bg-white"
            style={{ height: 480 }}
          />
        ) : tab === 'preview' && format === 'markdown' ? (
          <div className="md-preview px-4 py-4 overflow-auto" style={{ maxHeight: 480 }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
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

      {footer}
    </div>
  )
}
