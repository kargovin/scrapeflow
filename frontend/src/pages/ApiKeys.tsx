import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { apiGet, apiPost, apiDelete, type ApiKey, type ApiKeyCreated } from '../api'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  function handleCopy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <button
      onClick={handleCopy}
      className="px-3 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700"
    >
      {copied ? 'Copied!' : 'Copy'}
    </button>
  )
}

interface CreateModalProps {
  token: string
  onClose: () => void
}

function CreateModal({ token, onClose }: CreateModalProps) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [createdKey, setCreatedKey] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => apiPost<ApiKeyCreated>('/users/api-keys', token, { name }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      setCreatedKey(data.key)
    },
  })

  if (createdKey) {
    return (
      <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
        <div className="bg-white rounded-lg shadow-lg w-96 p-6">
          <h3 className="text-sm font-semibold text-gray-900 mb-1">API key created</h3>
          <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-4">
            Copy this key now — it will never be shown again.
          </p>
          <div className="flex items-center gap-2 bg-gray-50 border border-gray-200 rounded px-3 py-2 mb-4">
            <code className="text-xs text-gray-800 flex-1 break-all">{createdKey}</code>
            <CopyButton text={createdKey} />
          </div>
          <button
            onClick={onClose}
            className="w-full py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
          >
            Done
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-lg w-80 p-6">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Create API key</h3>
        <form
          onSubmit={e => { e.preventDefault(); mutation.mutate() }}
          className="space-y-3"
        >
          <label className="block">
            <span className="text-xs text-gray-600">Key name</span>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="e.g. cli-dev"
              required
              className="mt-1 w-full text-sm border border-gray-300 rounded px-2 py-1"
            />
          </label>
          <div className="flex gap-2 pt-1">
            <button
              type="submit"
              disabled={mutation.isPending || !name.trim()}
              className="flex-1 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
            >
              {mutation.isPending ? 'Creating…' : 'Create'}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-1.5 text-sm border border-gray-300 rounded hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
          {mutation.isError && (
            <p className="text-xs text-red-600">{String(mutation.error)}</p>
          )}
        </form>
      </div>
    </div>
  )
}

export default function ApiKeys() {
  const { getToken } = useAuth()
  const qc = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const { data: keys, isLoading, isError } = useQuery<ApiKey[]>({
    queryKey: ['api-keys'],
    queryFn: () => apiGet<ApiKey[]>('/users/api-keys', token!),
    enabled: !!token,
  })

  const revoke = useMutation({
    mutationFn: (id: string) => apiDelete(`/users/api-keys/${id}`, token!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['api-keys'] }),
  })

  const activeKeys = keys?.filter(k => !k.revoked) ?? []

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">API Keys</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
        >
          New key
        </button>
      </div>

      {isLoading || !token ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : isError ? (
        <p className="text-sm text-red-600">Failed to load API keys.</p>
      ) : activeKeys.length === 0 ? (
        <div className="bg-white border border-gray-200 rounded-lg p-8 text-center">
          <p className="text-sm text-gray-500 mb-3">No API keys yet.</p>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
          >
            Create your first key
          </button>
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {activeKeys.map(k => (
                <tr key={k.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-900 font-mono text-xs">{k.name}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {new Date(k.created_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => revoke.mutate(k.id)}
                      disabled={revoke.isPending}
                      className="text-xs text-red-600 hover:underline disabled:opacity-50"
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {revoke.isError && (
        <p className="mt-2 text-xs text-red-600">{String(revoke.error)}</p>
      )}

      {showCreate && token && (
        <CreateModal token={token} onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
