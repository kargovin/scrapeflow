import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@clerk/clerk-react'
import { apiGet, apiPatch, type AdminUser, type UserQuota } from '../api'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

interface QuotaFormProps {
  userId: string
  token: string
  onClose: () => void
}

function QuotaForm({ userId, token, onClose }: QuotaFormProps) {
  const qc = useQueryClient()
  const [monthly, setMonthly] = useState('')
  const [concurrent, setConcurrent] = useState('')
  const [storage, setStorage] = useState('')

  const { data: quota } = useQuery<UserQuota>({
    queryKey: ['quota', userId],
    queryFn: () => apiGet(`/admin/users/${userId}/quota`, token),
  })

  const mutation = useMutation({
    mutationFn: (body: Record<string, number | null>) =>
      apiPatch<UserQuota>(`/admin/users/${userId}/quota`, token, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['quota', userId] })
      onClose()
    },
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    mutation.mutate({
      monthly_runs_limit: monthly ? parseInt(monthly) : null,
      concurrent_jobs_limit: concurrent ? parseInt(concurrent) : null,
      storage_bytes_limit: storage ? parseInt(storage) * 1024 * 1024 * 1024 : null,
    })
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-lg w-80 p-6">
        <h3 className="text-sm font-semibold text-gray-900 mb-4">Edit Quota</h3>
        {quota && (
          <p className="text-xs text-gray-500 mb-3">
            Storage used: {formatBytes(quota.storage_bytes_used)}
          </p>
        )}
        <form onSubmit={submit} className="space-y-3">
          <label className="block">
            <span className="text-xs text-gray-600">Monthly runs limit (blank = default 500)</span>
            <input
              type="number"
              value={monthly}
              onChange={e => setMonthly(e.target.value)}
              placeholder={String(quota?.monthly_runs_limit ?? '')}
              className="mt-1 w-full text-sm border border-gray-300 rounded px-2 py-1"
            />
          </label>
          <label className="block">
            <span className="text-xs text-gray-600">Concurrent jobs (blank = default 5)</span>
            <input
              type="number"
              value={concurrent}
              onChange={e => setConcurrent(e.target.value)}
              placeholder={String(quota?.concurrent_jobs_limit ?? '')}
              className="mt-1 w-full text-sm border border-gray-300 rounded px-2 py-1"
            />
          </label>
          <label className="block">
            <span className="text-xs text-gray-600">Storage limit GB (blank = default 5 GB)</span>
            <input
              type="number"
              value={storage}
              onChange={e => setStorage(e.target.value)}
              placeholder={quota?.storage_bytes_limit ? String(quota.storage_bytes_limit / 1024 ** 3) : ''}
              className="mt-1 w-full text-sm border border-gray-300 rounded px-2 py-1"
            />
          </label>
          <div className="flex gap-2 pt-1">
            <button
              type="submit"
              disabled={mutation.isPending}
              className="flex-1 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
            >
              {mutation.isPending ? 'Saving…' : 'Save'}
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

export default function Users() {
  const { getToken } = useAuth()
  const [editingUserId, setEditingUserId] = useState<string | null>(null)

  const { data: token } = useQuery({
    queryKey: ['token'],
    queryFn: () => getToken() as Promise<string>,
    staleTime: 60_000,
  })

  const { data: users, isLoading, isError } = useQuery<AdminUser[]>({
    queryKey: ['admin-users'],
    queryFn: () => apiGet('/admin/users?limit=100', token!),
    enabled: !!token,
  })

  if (isLoading || !token) return <p className="text-sm text-gray-500">Loading users…</p>
  if (isError) return <p className="text-sm text-red-600">Failed to load users.</p>

  return (
    <div>
      <h2 className="text-lg font-semibold text-gray-900 mb-4">Users</h2>
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Email</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Admin</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Joined</th>
              <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {users?.map(u => (
              <tr key={u.id} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-gray-900">{u.email}</td>
                <td className="px-4 py-3">
                  {u.is_admin && (
                    <span className="inline-flex px-2 py-0.5 text-xs bg-indigo-100 text-indigo-700 rounded-full">
                      admin
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-500">
                  {new Date(u.created_at).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => setEditingUserId(u.id)}
                    className="text-xs text-indigo-600 hover:underline"
                  >
                    Edit quota
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editingUserId && token && (
        <QuotaForm
          userId={editingUserId}
          token={token}
          onClose={() => setEditingUserId(null)}
        />
      )}
    </div>
  )
}
