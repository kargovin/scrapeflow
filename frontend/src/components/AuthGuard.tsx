import { useUser, useAuth, RedirectToSignIn } from '@clerk/clerk-react'
import { useQuery } from '@tanstack/react-query'

interface Props {
  children: React.ReactNode
}

export default function AuthGuard({ children }: Props) {
  const { isSignedIn, isLoaded } = useUser()
  const { getToken, signOut } = useAuth()

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
    enabled: isSignedIn === true,
    retry: false,
    staleTime: 5 * 60_000,
  })

  if (!isLoaded || (isSignedIn && isLoading)) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500 text-sm">Loading…</p>
      </div>
    )
  }

  if (!isSignedIn) {
    return <RedirectToSignIn />
  }

  if (isError || isAdmin === false) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-center">
          <h1 className="text-xl font-semibold text-gray-900">Access denied</h1>
          <p className="mt-1 text-sm text-gray-500">Admin privileges required.</p>
          <button
            onClick={() => signOut()}
            className="mt-4 px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700"
          >
            Sign out
          </button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
