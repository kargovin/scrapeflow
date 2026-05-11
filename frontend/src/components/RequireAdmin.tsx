import { useAuth } from '@clerk/clerk-react'
import { useQuery } from '@tanstack/react-query'
import { Navigate, Outlet } from 'react-router-dom'

export default function RequireAdmin() {
  const { getToken } = useAuth()

  const { data: isAdmin, isLoading, isError } = useQuery({
    queryKey: ['admin-check'],
    queryFn: async () => {
      const token = await getToken()
      if (!token) return false
      const res = await fetch('/admin/users?limit=1', {
        headers: { Authorization: `Bearer ${token}` },
      })
      return res.ok
    },
    retry: false,
    staleTime: 5 * 60_000,
  })

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500 text-sm">Loading…</p>
      </div>
    )
  }

  if (isError || isAdmin === false) {
    return <Navigate to="/app/dashboard/api-keys" replace />
  }

  return <Outlet />
}
