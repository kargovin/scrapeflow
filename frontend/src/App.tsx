import { useState } from 'react'
import AuthGuard from './components/AuthGuard'
import Layout from './components/Layout'
import Users from './pages/Users'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import UsageStats from './pages/UsageStats'

export type Page = 'users' | 'jobs' | 'stats'

export default function App() {
  const [page, setPage] = useState<Page>('users')
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)

  function navigate(p: Page) {
    setPage(p)
    setSelectedJobId(null)
  }

  return (
    <AuthGuard>
      <Layout page={page} onNavigate={navigate}>
        {page === 'users' && <Users />}
        {page === 'jobs' && selectedJobId === null && (
          <Jobs onSelectJob={setSelectedJobId} />
        )}
        {page === 'jobs' && selectedJobId !== null && (
          <JobDetail jobId={selectedJobId} onBack={() => setSelectedJobId(null)} />
        )}
        {page === 'stats' && <UsageStats />}
      </Layout>
    </AuthGuard>
  )
}
