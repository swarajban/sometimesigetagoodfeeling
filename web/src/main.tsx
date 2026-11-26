import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router'
import './index.css'
import { Layout } from './components/Layout'
import { ClaudeLog } from './pages/ClaudeLog'
import { Import } from './pages/Import'
import { Overview } from './pages/Overview'
import { Projects } from './pages/Projects'
import { Review } from './pages/Review'
import { Settings } from './pages/Settings'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 2_000, refetchOnWindowFocus: true, retry: 1 } },
})

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <Projects /> },
      { path: '/import', element: <Import /> },
      { path: '/settings', element: <Settings /> },
      { path: '/p/:slug', element: <Overview /> },
      { path: '/p/:slug/review', element: <Review /> },
      { path: '/p/:slug/log', element: <ClaudeLog /> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
