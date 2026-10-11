import { useMemo } from 'react'
import { useAuth } from '@clerk/clerk-react'
import { apiDelete, apiGet, apiPatch, apiPost } from '../api'

// A fresh Clerk token per request: Clerk refreshes it before expiry, so nothing here can go stale
// the way a token cached in a query does (BUG-018).
export function useApi() {
  const { getToken } = useAuth()
  return useMemo(() => {
    const token = async () => {
      const t = await getToken()
      if (!t) throw new Error('Not signed in')
      return t
    }
    return {
      get: async <T,>(path: string) => apiGet<T>(path, await token()),
      post: async <T,>(path: string, body: unknown) => apiPost<T>(path, await token(), body),
      patch: async <T,>(path: string, body: unknown) => apiPatch<T>(path, await token(), body),
      del: async (path: string) => apiDelete(path, await token()),
      token,
    }
  }, [getToken])
}
