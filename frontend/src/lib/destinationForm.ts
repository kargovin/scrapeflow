import type { Destination, DestinationKind } from '../api'

// ADR-015 §2a: what each kind stores. `secret` fields are write-only: sent, never read back.
export interface FieldSpec {
  key: string
  label: string
  part: 'address' | 'secret'
  input: 'text' | 'password' | 'number' | 'textarea' | 'list' | 'select'
  optional?: boolean
  options?: string[]
  placeholder?: string
  // What an empty optional address field is stored as.
  empty?: null | ''
}

export const KINDS: DestinationKind[] = ['bucket', 'webhook', 'smtp', 'postgres']

export const KIND_LABELS: Record<DestinationKind, string> = {
  bucket: 'Bucket (S3-compatible)',
  webhook: 'Webhook',
  smtp: 'Email (SMTP)',
  postgres: 'Postgres table',
}

const CA: FieldSpec = {
  key: 'ca_certificate',
  label: 'CA certificate (PEM, optional — replaces the system trust)',
  part: 'address',
  input: 'textarea',
  optional: true,
  empty: null,
}
const LOGIN: FieldSpec[] = [
  { key: 'user', label: 'User', part: 'secret', input: 'text' },
  { key: 'password', label: 'Password', part: 'secret', input: 'password' },
]

export const FIELDS: Record<DestinationKind, FieldSpec[]> = {
  bucket: [
    { key: 'endpoint', label: 'Endpoint', part: 'address', input: 'text', placeholder: 'https://s3.eu-west-1.amazonaws.com' },
    { key: 'bucket', label: 'Bucket', part: 'address', input: 'text' },
    { key: 'prefix', label: 'Key prefix (optional)', part: 'address', input: 'text', optional: true, empty: '' },
    { key: 'region', label: 'Region (optional)', part: 'address', input: 'text', optional: true, empty: null },
    { key: 'access_key', label: 'Access key', part: 'secret', input: 'text' },
    { key: 'secret_key', label: 'Secret key', part: 'secret', input: 'password' },
  ],
  webhook: [
    { key: 'url', label: 'URL (https; shown by its host only once saved)', part: 'secret', input: 'password', placeholder: 'https://hooks.example.com/…' },
    { key: 'secret', label: 'Signing secret (optional)', part: 'secret', input: 'password', optional: true },
  ],
  smtp: [
    { key: 'host', label: 'Mail server host', part: 'address', input: 'text' },
    { key: 'port', label: 'Port', part: 'address', input: 'number', placeholder: '587' },
    { key: 'tls_mode', label: 'TLS', part: 'address', input: 'select', options: ['starttls', 'implicit'] },
    { key: 'from', label: 'From', part: 'address', input: 'text' },
    { key: 'to', label: 'To (one or more, separated by commas or lines)', part: 'address', input: 'list' },
    CA,
    ...LOGIN,
  ],
  postgres: [
    { key: 'host', label: 'Host', part: 'address', input: 'text' },
    { key: 'port', label: 'Port', part: 'address', input: 'number', placeholder: '5432' },
    { key: 'database', label: 'Database', part: 'address', input: 'text' },
    { key: 'schema', label: 'Schema', part: 'address', input: 'text', placeholder: 'public' },
    { key: 'table', label: 'Table', part: 'address', input: 'text' },
    CA,
    ...LOGIN,
  ],
}

export type Values = Record<string, string>

export function initialValues(kind: DestinationKind, stored?: Destination): Values {
  const values: Values = {}
  for (const f of FIELDS[kind]) {
    const v = f.part === 'address' ? stored?.address[f.key] : undefined
    if (Array.isArray(v)) values[f.key] = v.join(', ')
    else if (v !== undefined && v !== null) values[f.key] = String(v)
    else if (!stored && f.input === 'select') values[f.key] = f.options![0]
    else values[f.key] = ''
  }
  return values
}

function parsed(f: FieldSpec, raw: string): unknown {
  const v = f.input === 'textarea' ? raw : raw.trim()
  if (f.input === 'number') return Number(v)
  if (f.input === 'list') return v.split(/[\s,;]+/).filter(Boolean)
  return v
}

function stored(value: unknown): string {
  if (Array.isArray(value)) return value.join(', ')
  return value === undefined || value === null ? '' : String(value)
}

// A new destination's form: empty optional fields are left to the API's defaults.
export function newForm(kind: DestinationKind, values: Values) {
  const address: Record<string, unknown> = {}
  const secret: Record<string, unknown> = {}
  for (const f of FIELDS[kind]) {
    if (!values[f.key]?.trim()) continue
    ;(f.part === 'address' ? address : secret)[f.key] = parsed(f, values[f.key])
  }
  return { kind, address, secret }
}

// An edit, as the PATCH merges it: changed address fields only; a blank secret field keeps its value.
export function editForm(kind: DestinationKind, values: Values, current: Destination) {
  const address: Record<string, unknown> = {}
  const secret: Record<string, unknown> = {}
  for (const f of FIELDS[kind]) {
    const raw = values[f.key] ?? ''
    if (f.part === 'secret') {
      if (raw.trim()) secret[f.key] = parsed(f, raw)
      continue
    }
    if (raw.trim() === stored(current.address[f.key]).trim()) continue
    address[f.key] = raw.trim() ? parsed(f, raw) : (f.empty ?? null)
  }
  return {
    address: Object.keys(address).length ? address : undefined,
    secret: Object.keys(secret).length ? secret : undefined,
  }
}

// What a read shows of a stored address, by field label; a webhook's has nothing (its URL is a secret).
export function addressRows(d: Destination): [string, string][] {
  return FIELDS[d.kind]
    .filter(f => f.part === 'address')
    .map(f => [f.label.replace(/ \(.*\)$/, ''), stored(d.address[f.key]) || '—'] as [string, string])
    .map(([label, value]) => [label, label.startsWith('CA certificate') && value !== '—' ? 'set' : value])
}
