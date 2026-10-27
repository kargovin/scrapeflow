import { useQuery } from '@tanstack/react-query'
import { apiGet, type JobResult } from '../api'
import ContentViewer from './ContentViewer'

interface Props {
  jobId: string
  outputFormat: string
  status: string
  resultPath: string | null
  token: string
  mode: 'admin' | 'user'
}

export default function ResultViewer({ jobId, outputFormat, status, resultPath, token, mode }: Props) {
  const apiBase = mode === 'admin' ? '/admin/jobs' : '/jobs'
  const enabled = status === 'completed' && !!resultPath && !!token
  const { data, isLoading, isError, error } = useQuery<JobResult>({
    queryKey: ['job-result', mode, jobId],
    queryFn: () => apiGet(`${apiBase}/${jobId}/result`, token),
    enabled,
  })

  const format = data?.output_format ?? outputFormat

  return (
    <ContentViewer
      title="Result"
      subtitle={
        <span className="flex items-center gap-2">
          <span className="inline-flex px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-600">
            {format}
          </span>
          {data?.completed_at && (
            <span className="text-xs text-gray-500">
              latest completed run · {new Date(data.completed_at).toLocaleString()}
            </span>
          )}
        </span>
      }
      format={format}
      content={enabled && data ? data.content : null}
      placeholder={
        !enabled ? (
          <p className="px-4 py-6 text-sm text-gray-500">No result available for this status.</p>
        ) : isLoading ? (
          <p className="px-4 py-6 text-sm text-gray-500">Loading result…</p>
        ) : isError ? (
          <p className="px-4 py-6 text-sm text-red-600">Failed to load result: {String(error)}</p>
        ) : null
      }
      filename={jobId}
      footer={
        data?.warnings && data.warnings.length > 0 && (
          <div className="border-t border-gray-100 px-4 py-2 bg-amber-50">
            <p className="text-xs font-medium text-amber-800 mb-1">Warnings</p>
            <ul className="list-disc pl-5 text-xs text-amber-700">
              {data.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        )
      }
    />
  )
}
