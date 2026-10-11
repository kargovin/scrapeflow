import type { SocketState } from '../lib/useLiveSocket'

const LABELS: Partial<Record<SocketState, [string, string, string]>> = {
  live: ['live', 'text-green-600', 'bg-green-500 animate-pulse'],
  connecting: ['connecting…', 'text-gray-500', 'bg-gray-400'],
  reconnecting: ['reconnecting…', 'text-amber-600', 'bg-amber-500'],
  limited: ['too many live views — retrying', 'text-amber-600', 'bg-amber-500'],
}

export default function LiveBadge({ state }: { state: SocketState }) {
  const label = LABELS[state]
  if (!label) return null
  const [text, colour, dot] = label
  return (
    <span className={`flex items-center gap-1 text-xs ${colour}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      {text}
    </span>
  )
}
