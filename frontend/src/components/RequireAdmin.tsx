import { Navigate, Outlet } from 'react-router-dom'
import { useIsAdmin } from '../lib/useIsAdmin'

export default function RequireAdmin() {
  const { data: isAdmin, isLoading, isError } = useIsAdmin()

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
