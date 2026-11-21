import type { DestinationKind } from '../api'
import { FIELDS, type Values } from '../lib/destinationForm'

const INPUT = 'block mt-1 w-full text-sm border border-gray-300 rounded px-2 py-1 text-gray-900'

// One kind's fields. On an edit the secret fields start empty: blank keeps the stored value.
export default function DestinationFields({
  kind,
  values,
  onChange,
  editing = false,
}: {
  kind: DestinationKind
  values: Values
  onChange: (values: Values) => void
  editing?: boolean
}) {
  const set = (key: string, value: string) => onChange({ ...values, [key]: value })
  const fields = FIELDS[kind]
  const section = (part: 'address' | 'secret') => fields.filter(f => f.part === part)
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {(['address', 'secret'] as const).map(part =>
        section(part).length === 0 ? null : (
          <fieldset key={part} className="space-y-3">
            <legend className="text-xs font-medium text-gray-500 uppercase mb-1">
              {part === 'address' ? 'Address' : 'Credentials'}
            </legend>
            {part === 'secret' && (
              <p className="text-xs text-gray-500">
                Stored encrypted and never shown again
                {editing ? '. Leave a field blank to keep what is stored; fill it to replace it.' : '.'}
              </p>
            )}
            {section(part).map(f => (
              <label key={f.key} className="block text-xs text-gray-600">
                {f.label}
                {f.input === 'textarea' ? (
                  <textarea
                    value={values[f.key] ?? ''}
                    onChange={e => set(f.key, e.target.value)}
                    rows={4}
                    className={`${INPUT} font-mono text-xs`}
                  />
                ) : f.input === 'list' ? (
                  <textarea
                    value={values[f.key] ?? ''}
                    onChange={e => set(f.key, e.target.value)}
                    rows={2}
                    className={INPUT}
                  />
                ) : f.input === 'select' ? (
                  <select value={values[f.key] ?? ''} onChange={e => set(f.key, e.target.value)} className={INPUT}>
                    {f.options!.map(o => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type={f.input === 'number' ? 'number' : f.input}
                    value={values[f.key] ?? ''}
                    onChange={e => set(f.key, e.target.value)}
                    placeholder={editing && f.part === 'secret' ? 'unchanged' : f.placeholder}
                    autoComplete={f.part === 'secret' ? 'new-password' : 'off'}
                    className={INPUT}
                  />
                )}
              </label>
            ))}
          </fieldset>
        ),
      )}
    </div>
  )
}
