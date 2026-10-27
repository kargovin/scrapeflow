// Plain words for the cron shapes people write; anything else is shown as the expression.
// Schedules run in UTC (the API's schedule_cron contract).

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

const num = (s: string) => /^\d+$/.test(s)
const pad = (s: string) => s.padStart(2, '0')

export function describeCron(expr: string): string {
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return expr
  const [min, hour, dom, mon, dow] = parts
  const at = num(min) && num(hour) ? `${pad(hour)}:${pad(min)} UTC` : null

  if (mon !== '*') return expr
  if (/^\*\/\d+$/.test(min) && hour === '*' && dom === '*' && dow === '*') {
    return `Every ${min.slice(2)} minutes`
  }
  if (num(min) && hour === '*' && dom === '*' && dow === '*') {
    return `Every hour at :${pad(min)}`
  }
  if (num(min) && /^\*\/\d+$/.test(hour) && dom === '*' && dow === '*') {
    return `Every ${hour.slice(2)} hours at :${pad(min)}`
  }
  if (at && dom === '*' && dow === '*') return `Every day at ${at}`
  if (at && dom === '*' && num(dow) && Number(dow) <= 7) return `Every ${DAYS[Number(dow) % 7]} at ${at}`
  if (at && dom === '*' && dow === '1-5') return `Weekdays at ${at}`
  if (at && num(dom) && dow === '*') return `Monthly on day ${dom} at ${at}`
  return expr
}

// ADR-010 §1: why a tick made no run.
export const SKIP_REASONS: Record<string, string> = {
  monthly_runs: 'the monthly run limit was reached',
  concurrent_jobs: 'no concurrency slot freed within a day',
}
