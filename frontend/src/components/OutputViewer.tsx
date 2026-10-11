import { useMemo, useState } from 'react'
import Editor from '@monaco-editor/react'
import '../lib/monaco' // side-effect: point Monaco at locally-bundled assets

const LANGUAGE_BY_EXT: Record<string, string> = { json: 'json', md: 'markdown', html: 'html' }

function pretty(raw: string, language: string): string {
  if (language !== 'json') return raw
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

interface Props {
  title: string
  path: string
  content: string
}

// Read-only view of one stored object; the language comes from the object's extension.
export default function OutputViewer({ title, path, content }: Props) {
  const [copied, setCopied] = useState(false)
  const language = LANGUAGE_BY_EXT[path.split('.').pop() ?? ''] ?? 'plaintext'
  const source = useMemo(() => pretty(content, language), [content, language])

  const copy = async () => {
    await navigator.clipboard.writeText(source)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden mb-6">
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          <span className="text-xs text-gray-400 truncate" title={path}>{path}</span>
        </div>
        <button
          onClick={copy}
          className="px-2 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <Editor
        height="420px"
        language={language}
        value={source}
        options={{ readOnly: true, minimap: { enabled: false }, wordWrap: 'on', fontSize: 12 }}
      />
    </div>
  )
}
