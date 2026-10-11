import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@clerk/clerk-react'
import { API_BASE } from '../api'

// ADR-009 §11b: reconnect on any close that did not follow a terminal message. Each connect
// gets a fresh token and the server's first message is the current state, so a reconnect also
// re-reads whatever was missed while disconnected.
const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 30_000]
const LIMIT_BACKOFF_MS = 30_000
const NOT_FOUND = 4004
const LIMIT = 4029

export type SocketState = 'connecting' | 'live' | 'reconnecting' | 'limited' | 'closed'

interface Options<M> {
  // e.g. `/jobs/${id}/watch`; null = do not connect.
  path: string | null
  onMessage: (msg: M) => void
  isTerminal: (msg: M) => boolean
}

export function useLiveSocket<M extends { type?: string }>({ path, onMessage, isTerminal }: Options<M>) {
  const { getToken } = useAuth()
  const [state, setState] = useState<SocketState>('closed')
  // Latest callbacks without re-running the effect on every render.
  const handlers = useRef({ onMessage, isTerminal })
  handlers.current = { onMessage, isTerminal }

  useEffect(() => {
    if (!path) return
    let ws: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let stopped = false

    const base = (API_BASE || window.location.origin).replace(/^http/, 'ws')

    const schedule = (delay: number, next: SocketState) => {
      if (stopped) return
      setState(next)
      timer = setTimeout(connect, delay)
    }

    const connect = async () => {
      setState(attempt === 0 ? 'connecting' : 'reconnecting')
      const token = await getToken().catch(() => null)
      if (stopped) return
      if (!token) {
        schedule(BACKOFF_MS[Math.min(attempt++, BACKOFF_MS.length - 1)], 'reconnecting')
        return
      }
      let finished = false
      ws = new WebSocket(`${base}${path}?token=${encodeURIComponent(token)}`)
      ws.onopen = () => {
        attempt = 0
        setState('live')
      }
      ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data) as M
        handlers.current.onMessage(msg)
        if (handlers.current.isTerminal(msg)) finished = true
      }
      ws.onerror = () => ws?.close()
      ws.onclose = (evt) => {
        if (stopped) return
        if (finished || evt.code === NOT_FOUND) {
          setState('closed')
          return
        }
        if (evt.code === LIMIT) {
          schedule(LIMIT_BACKOFF_MS, 'limited')
          return
        }
        // A server `timeout` message, a dropped connection, a deploy: all reconnect.
        schedule(BACKOFF_MS[Math.min(attempt++, BACKOFF_MS.length - 1)], 'reconnecting')
      }
    }

    connect()
    return () => {
      stopped = true
      clearTimeout(timer)
      ws?.close()
      setState('closed')
    }
  }, [path, getToken])

  return state
}
