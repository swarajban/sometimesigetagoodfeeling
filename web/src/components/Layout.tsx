import { FolderOpen, Plus, Settings as SettingsIcon } from 'lucide-react'
import { NavLink, Outlet, useMatch } from 'react-router'
import { cx } from '../lib/format'

function Tab({ to, children, end }: { to: string; children: React.ReactNode; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cx(
          'flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[13px] font-medium transition-colors',
          isActive ? 'bg-white/8 text-zinc-50' : 'text-zinc-400 hover:text-zinc-100',
        )
      }
    >
      {children}
    </NavLink>
  )
}

export function Layout() {
  const project = useMatch('/p/:slug/*')
  const slug = project?.params.slug
  return (
    <div className="flex h-full flex-col">
      <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-4 border-b border-white/6 bg-ink-950/90 px-4 backdrop-blur">
        <NavLink to="/" className="flex items-center gap-2.5">
          <svg viewBox="0 0 32 32" className="size-6">
            <rect width="32" height="32" rx="7" fill="#1c1c21" />
            <path d="M6 21c3-7 6-10 10-10s7 3 10 10" fill="none" stroke="#f5b14a" strokeWidth="3" strokeLinecap="round" />
            <circle cx="16" cy="21" r="2.5" fill="#f5b14a" />
          </svg>
          <span className="text-[13.5px] font-semibold tracking-tight text-zinc-100">Sometimes I Get a Good Feeling</span>
        </NavLink>
        <nav className="flex items-center gap-1">
          <Tab to="/" end>
            <FolderOpen className="size-3.5" /> Projects
          </Tab>
          {slug && (
            <>
              <span className="px-1 text-zinc-700">/</span>
              <Tab to={`/p/${slug}`} end>
                {slug}
              </Tab>
              <Tab to={`/p/${slug}/review`}>Review</Tab>
              <Tab to={`/p/${slug}/log`}>Claude log</Tab>
            </>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-1">
          <Tab to="/import">
            <Plus className="size-3.5" /> Import
          </Tab>
          <Tab to="/settings">
            <SettingsIcon className="size-3.5" /> Settings
          </Tab>
        </div>
      </header>
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}
