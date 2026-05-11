import { useUser, RedirectToSignIn } from '@clerk/clerk-react'
import { Outlet } from 'react-router-dom'

export default function RequireSignedIn() {
  const { isSignedIn, isLoaded } = useUser()

  if (!isLoaded) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <p className="text-gray-500 text-sm">Loading…</p>
      </div>
    )
  }

  if (!isSignedIn) {
    return <RedirectToSignIn />
  }

  return <Outlet />
}
