import { useQuery } from '@tanstack/react-query'
import { type Approval } from '../api'
import { useApi } from './useApi'

// Approvals still waiting (PRD-018 R9). The user's list is unpaged and shared by every page;
// an admin's is read per monitor, or the first `limit` across all users.
export function useWaitingApprovals({
  mode,
  monitorId,
  enabled = true,
  limit = 200,
  offset = 0,
  refetchInterval,
}: {
  mode: 'admin' | 'user'
  monitorId?: string | null
  enabled?: boolean
  limit?: number
  offset?: number
  refetchInterval?: number
}) {
  const api = useApi()
  const admin = mode === 'admin'
  const path = !admin
    ? '/approvals'
    : monitorId
      ? `/admin/approvals?monitor_id=${monitorId}`
      : `/admin/approvals?limit=${limit}&offset=${offset}`
  const query = useQuery<Approval[]>({
    queryKey: admin ? ['approvals', 'admin', path] : ['approvals', 'user'],
    queryFn: () => api.get(path),
    enabled,
    refetchInterval,
  })
  const data = monitorId && query.data ? query.data.filter(a => a.monitor_id === monitorId) : query.data
  return { ...query, data }
}
