import { useAuth } from '@clerk/clerk-react'
import { useQuery } from '@tanstack/react-query'

// Admin status is inferred by probing an admin-only endpoint (there is no role
// claim on the Clerk JWT). Shared by RequireAdmin (route gate) and Layout (nav
// cross-link) — the ['admin-check'] query key means both read one cached result.
export function useIsAdmin() {
  const { getToken } = useAuth()

  return useQuery({
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
}
