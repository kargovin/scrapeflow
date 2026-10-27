import { lazy, Suspense } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { JobResult } from '../api'
import { useApi } from '../lib/useApi'
import ErrorBox from './ErrorBox'

// Monaco in its own chunk, as on the job page.
const ContentViewer = lazy(() => import('./ContentViewer'))

// One batch item's or crawl page's stored output, read from `path` (its `/result` route).
export default function StoredResult({ path, title, filename }: { path: string; title: string; filename: string }) {
  const api = useApi()
  const result = useQuery<JobResult>({
    queryKey: ['stored-result', path],
    queryFn: () => api.get(path),
    retry: false,
  })
  if (result.isLoading) return <p className="mb-6 text-sm text-gray-500">Loading output…</p>
  if (result.isError) return <div className="mb-6"><ErrorBox error={result.error} /></div>
  if (!result.data) return null
  return (
    <Suspense fallback={<p className="mb-6 text-sm text-gray-500">Loading viewer…</p>}>
      <ContentViewer
        key={result.data.result_path}
        title={title}
        subtitle={
          <span className="text-xs text-gray-400 truncate" title={result.data.result_path}>
            {result.data.result_path}
          </span>
        }
        format={result.data.output_format}
        content={result.data.content}
        filename={filename}
      />
    </Suspense>
  )
}
