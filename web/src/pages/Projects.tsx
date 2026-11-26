import { Clapperboard, Plus } from 'lucide-react'
import { Link } from 'react-router'
import { useProjects } from '../api/hooks'
import { Badge, Empty, ErrorNote, Loading } from '../components/ui'
import { ago, clockShort, usd } from '../lib/format'

export function Projects() {
  const { data, isLoading, error } = useProjects()
  return (
    <div className="mx-auto max-w-[1500px] px-6 py-6">
      <div className="mb-5 flex items-end justify-between">
        <div>
          <h1 className="text-[18px] font-semibold text-zinc-50">Projects</h1>
          <p className="mt-0.5 text-zinc-500">Every session you've imported. Opening one never re-scores it.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      {isLoading && <Loading />}
      {data && data.length === 0 && (
        <Empty title="No projects yet">
          <Link to="/import" className="text-accent hover:underline">
            Import a footage folder
          </Link>{' '}
          and a music track to make your first edit.
        </Empty>
      )}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-4">
        {data?.map((p) => (
          <Link
            key={p.slug}
            to={`/p/${p.slug}`}
            className="group overflow-hidden rounded-lg bg-ink-900 ring-1 ring-white/6 transition hover:ring-white/15"
          >
            <div className="relative aspect-video bg-ink-950">
              {p.thumb_url ? (
                <img src={p.thumb_url} alt="" className="size-full object-cover opacity-90 transition group-hover:opacity-100" />
              ) : (
                <div className="flex size-full items-center justify-center text-zinc-700">
                  <Clapperboard className="size-8" />
                </div>
              )}
              {p.active_job && (
                <div className="absolute top-2 left-2">
                  <Badge tone={p.active_job.status === 'approval' ? 'accent' : 'info'} className="backdrop-blur">
                    {p.active_job.status === 'approval' ? 'Waiting for approval' : `${p.active_job.stage ?? 'queued'}…`}
                  </Badge>
                </div>
              )}
            </div>
            <div className="space-y-2 p-3.5">
              <div className="flex items-baseline justify-between gap-3">
                <div className="truncate text-[14px] font-semibold text-zinc-100">{p.name}</div>
                <div className="shrink-0 text-[12px] text-zinc-400 tabular-nums" title="Claude API spend on this project">
                  {usd(p.cost_usd)}
                </div>
              </div>
              <div className="truncate font-mono text-[11px] text-zinc-500" title={p.footage_dir}>
                {p.footage_dir}
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                {p.edits.map((e) => (
                  <Badge key={e.name} tone={e.final_url ? 'default' : 'warn'}>
                    {e.name} · {e.format === 'reel' ? '9:16' : '16:9'} · {clockShort(e.duration_s)}
                  </Badge>
                ))}
                {p.edits.length === 0 && <Badge tone="warn">no edit yet</Badge>}
                <span className="ml-auto text-[11.5px] text-zinc-500">
                  {p.clips} clips · {p.segments} segments · {ago(p.updated_at)}
                </span>
              </div>
            </div>
          </Link>
        ))}
        {data && data.length > 0 && (
          <Link
            to="/import"
            className="flex min-h-48 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-white/10 text-zinc-500 hover:border-white/25 hover:text-zinc-300"
          >
            <Plus className="size-5" /> Import footage
          </Link>
        )}
      </div>
    </div>
  )
}
