import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import RequireSignedIn from './components/RequireSignedIn'
import RequireAdmin from './components/RequireAdmin'
import Layout from './components/Layout'
import Users from './pages/Users'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import UsageStats from './pages/UsageStats'
import ApiKeys from './pages/ApiKeys'
import Pipelines from './pages/Pipelines'
import PipelineDetail from './pages/PipelineDetail'
import PipelineRunDetail from './pages/PipelineRunDetail'

// Monaco stays out of the main chunk (frontend asset rule).
const PipelineEditor = lazy(() => import('./pages/PipelineEditor'))
const editor = (
  <Suspense fallback={<p className="text-sm text-gray-500">Loading editor…</p>}>
    <PipelineEditor />
  </Suspense>
)

const ADMIN_NAV = [
  { path: '/app/admin/users', label: 'Users' },
  { path: '/app/admin/jobs', label: 'Jobs' },
  { path: '/app/admin/pipelines', label: 'Pipelines' },
  { path: '/app/admin/stats', label: 'Usage Stats' },
]

const USER_NAV = [
  { path: '/app/dashboard/jobs', label: 'Jobs' },
  { path: '/app/dashboard/pipelines', label: 'Pipelines' },
  { path: '/app/dashboard/api-keys', label: 'API Keys' },
]

export default function App() {
  return (
    <Routes>
      <Route element={<RequireSignedIn />}>

        {/* Admin subtree — redirect to /app/dashboard/api-keys if not admin */}
        <Route element={<RequireAdmin />}>
          <Route element={<Layout navItems={ADMIN_NAV} title="ScrapeFlow Admin" variant="admin" />}>
            <Route path="/app/admin" element={<Navigate to="/app/admin/users" replace />} />
            <Route path="/app/admin/users" element={<Users />} />
            <Route path="/app/admin/jobs" element={<Jobs mode="admin" />} />
            <Route path="/app/admin/jobs/:jobId" element={<JobDetail mode="admin" />} />
            <Route path="/app/admin/pipelines" element={<Pipelines mode="admin" />} />
            <Route path="/app/admin/pipelines/:pipelineId" element={<PipelineDetail mode="admin" />} />
            <Route
              path="/app/admin/pipelines/:pipelineId/runs/:runId"
              element={<PipelineRunDetail mode="admin" />}
            />
            <Route path="/app/admin/stats" element={<UsageStats />} />
          </Route>
        </Route>

        {/* User dashboard — available to all signed-in users */}
        <Route element={<Layout navItems={USER_NAV} title="ScrapeFlow" variant="user" />}>
          <Route path="/app/dashboard" element={<Navigate to="/app/dashboard/jobs" replace />} />
          <Route path="/app/dashboard/jobs" element={<Jobs mode="user" />} />
          <Route path="/app/dashboard/jobs/:jobId" element={<JobDetail mode="user" />} />
          <Route path="/app/dashboard/pipelines" element={<Pipelines mode="user" />} />
          <Route path="/app/dashboard/pipelines/new" element={editor} />
          <Route path="/app/dashboard/pipelines/:pipelineId" element={<PipelineDetail mode="user" />} />
          <Route path="/app/dashboard/pipelines/:pipelineId/edit" element={editor} />
          <Route
            path="/app/dashboard/pipelines/:pipelineId/runs/:runId"
            element={<PipelineRunDetail mode="user" />}
          />
          <Route path="/app/dashboard/api-keys" element={<ApiKeys />} />
        </Route>

      </Route>
    </Routes>
  )
}
