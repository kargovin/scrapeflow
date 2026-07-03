import { useAuth } from '@clerk/clerk-react'
import { useNavigate, useLocation, Outlet } from 'react-router-dom'
import { useIsAdmin } from '../lib/useIsAdmin'

interface NavItem {
  path: string
  label: string
}

interface Props {
  navItems: NavItem[]
  title?: string
  // Which shell this is — drives the cross-link between the admin panel and the
  // personal dashboard. Omit to render no cross-link.
  variant?: 'admin' | 'user'
}

export default function Layout({ navItems, title = 'ScrapeFlow', variant }: Props) {
  const { signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { data: isAdmin } = useIsAdmin()

  // Admin panel always offers a way back to the personal dashboard. The user
  // dashboard only reveals the admin panel to users who actually are admins.
  const crossLink =
    variant === 'admin'
      ? { path: '/app/dashboard/jobs', label: '← My dashboard' }
      : variant === 'user' && isAdmin
        ? { path: '/app/admin', label: 'Admin panel →' }
        : null

  return (
    <div className="h-screen flex bg-gray-50">
      {/* Sidebar */}
      <aside className="w-52 bg-white border-r border-gray-200 flex flex-col">
        <div className="px-4 py-5 border-b border-gray-200">
          <span className="text-sm font-semibold text-indigo-600">{title}</span>
        </div>
        <nav className="flex-1 py-4 space-y-1 px-2">
          {navItems.map(({ path, label }) => (
            <button
              key={path}
              onClick={() => navigate(path)}
              className={[
                'w-full text-left px-3 py-2 rounded text-sm font-medium transition-colors',
                location.pathname.startsWith(path)
                  ? 'bg-indigo-50 text-indigo-700'
                  : 'text-gray-600 hover:bg-gray-100',
              ].join(' ')}
            >
              {label}
            </button>
          ))}
        </nav>
        {crossLink && (
          <div className="px-2 py-2 border-t border-gray-200">
            <button
              onClick={() => navigate(crossLink.path)}
              className="w-full text-left px-3 py-2 rounded text-sm font-medium text-gray-500 hover:bg-gray-100"
            >
              {crossLink.label}
            </button>
          </div>
        )}
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
        <Outlet />
      </main>
    </div>
  )
}
