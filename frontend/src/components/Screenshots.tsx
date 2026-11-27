import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Screenshot } from '../api'
import { useApi } from '../lib/useApi'

// The PNG routes need the Clerk token, which an <img src> cannot send: fetch, then a blob URL.
function Shot({ path, label, filename }: { path: string; label: string; filename: string }) {
  const api = useApi()
  const png = useQuery<Blob>({
    queryKey: ['screenshot', path],
    queryFn: () => api.blob(path),
    staleTime: Infinity,
    retry: false,
  })
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!png.data) return
    const u = URL.createObjectURL(png.data)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [png.data])

  return (
    <figure className="border border-gray-200 rounded-md overflow-hidden bg-white">
      {url ? (
        <a href={url} target="_blank" rel="noreferrer" title="Open full size">
          <img src={url} alt={label} className="block w-full h-40 object-cover object-top" />
        </a>
      ) : (
        <div className="h-40 flex items-center justify-center text-xs text-gray-400">
          {png.isError ? 'Failed to load' : 'Loading…'}
        </div>
      )}
      <figcaption className="flex items-center justify-between px-2 py-1 text-xs text-gray-600 border-t border-gray-100">
        <span>{label}</span>
        {url && (
          <a href={url} download={filename} className="text-indigo-600 hover:underline">
            Download
          </a>
        )}
      </figcaption>
    </figure>
  )
}

// A run's (or pipeline block's) screenshots; `base` is the route up to `/screenshots`.
export default function Screenshots({ base, screenshots, filename }: { base: string; screenshots: Screenshot[]; filename: string }) {
  if (screenshots.length === 0) return null
  return (
    <div className="border-t border-gray-100 px-4 py-3">
      <p className="text-xs font-medium text-gray-700 mb-2">Screenshots ({screenshots.length})</p>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        {screenshots.map(s => (
          <Shot
            key={s.index}
            path={`${base}/screenshots/${s.index}`}
            label={`#${s.index + 1} · ${Math.max(1, Math.round(s.bytes / 1024))} KB`}
            filename={`${filename}-screenshot-${s.index + 1}.png`}
          />
        ))}
      </div>
    </div>
  )
}
