import { useAuth } from '@clerk/clerk-react'
import type { Page } from '../App'

const NAV: { id: Page; label: string }[] = [
  { id: 'users', label: 'Users' },
  { id: 'jobs', label: 'Jobs' },
  { id: 'stats', label: 'Usage Stats' },
]

interface Props {
  page: Page
  onNavigate: (p: Page) => void
  children: React.ReactNode
}

export default function Layout({ page, onNavigate, children }: Props) {
  const { signOut } = useAuth()

  return (
    <div className="min-h-screen flex bg-gray-50">
      {/* Sidebar */}
      <aside className="w-52 bg-white border-r border-gray-200 flex flex-col">
        <div className="px-4 py-5 border-b border-gray-200">
          <span className="text-sm font-semibold text-indigo-600">ScrapeFlow Admin</span>
        </div>
        <nav className="flex-1 py-4 space-y-1 px-2">
          {NAV.map(({ id, label }) => (
            <button
              key={id}
              onClick={() => onNavigate(id)}
              className={[
                'w-full text-left px-3 py-2 rounded text-sm font-medium transition-colors',
                page === id
                  ? 'bg-indigo-50 text-indigo-700'
                  : 'text-gray-600 hover:bg-gray-100',
              ].join(' ')}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="px-4 py-4 border-t border-gray-200">
          <button
            onClick={() => signOut()}
            className="w-full text-left text-xs text-gray-400 hover:text-gray-600"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto p-8">
        {children}
      </main>
    </div>
  )
}
