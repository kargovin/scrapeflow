// Configure Monaco to load from locally-bundled assets instead of a CDN.
//
// The default @monaco-editor/react loader fetches Monaco from jsdelivr at
// runtime. The admin SPA is served from `/app/` with no guarantee of external
// network access, so we bundle Monaco (and its language web workers) with Vite
// and point the loader at that local copy.
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'

// Vite resolves each `?worker` import to a constructor that spins up a bundled
// worker chunk. Markdown has no dedicated language worker — it falls back to the
// base editor worker.
;(self as unknown as { MonacoEnvironment: monaco.Environment }).MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    if (label === 'json') return new jsonWorker()
    if (label === 'html') return new htmlWorker()
    return new editorWorker()
  },
}

loader.config({ monaco })
