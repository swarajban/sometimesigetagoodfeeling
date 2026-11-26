import { ListChecks, RefreshCw, ScrollText, Star } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import type { EditDetail, ProjectDetail } from '../api/client'
import { useEdit, useProject, useReplan } from '../api/hooks'
import { JobCard } from '../components/JobCard'
import { MusicStrip } from '../components/MusicStrip'
import { Badge, Button, Empty, ErrorNote, Loading, Panel, Segmented } from '../components/ui'
import { clock, clockShort, cx, usd } from '../lib/format'

export function Overview() {
  const { slug = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const project = useProject(slug)
  const editName = params.get('edit') ?? project.data?.edits[0]?.name
  const edit = useEdit(slug, project.data?.edits.some((e) => e.name === editName) ? editName : undefined)
  const replan = useReplan(slug)

  if (project.isLoading) return <Loading />
  if (project.error || !project.data) return <div className="p-6"><ErrorNote error={project.error} /></div>
  const p = project.data
  const active = p.active_job

  return (
    <div className="mx-auto max-w-[1600px] space-y-4 px-6 py-5">
      <div className="flex flex-wrap items-center gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-[18px] font-semibold text-zinc-50">{p.name}</h1>
          <div className="truncate font-mono text-[11.5px] text-zinc-500">{p.footage_dir}</div>
        </div>
        <div className="flex items-center gap-3 text-[12px] text-zinc-400">
          <span>{p.clips} clips</span>
          <span>{p.segments} segments</span>
          <span title="Claude API spend on this project, all runs">Claude {usd(p.cost_usd)}</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {p.edits.length > 1 && editName && (
            <Segmented
              value={editName}
              onChange={(v) => setParams({ edit: v })}
              options={p.edits.map((e) => ({ value: e.name, label: `${e.name} · ${e.format === 'reel' ? '9:16' : '16:9'}` }))}
            />
          )}
          <Link to={`/p/${slug}/review`}>
            <Button>
              <ListChecks className="size-4" /> Review segments
            </Button>
          </Link>
          <Link to={`/p/${slug}/log`}>
            <Button tone="ghost">
              <ScrollText className="size-4" /> Claude log
            </Button>
          </Link>
          {editName && (
            <Button onClick={() => replan.mutate(editName)} busy={replan.isPending} disabled={!!active} title="Run the director again with your review choices, then render">
              <RefreshCw className="size-4" /> Re-plan
            </Button>
          )}
        </div>
      </div>
      <ErrorNote error={replan.error} />
      {active && <JobCard key={active.id} job={active} />}
      {!active && p.jobs[0] && p.jobs[0].status === 'failed' && <JobCard key={p.jobs[0].id} job={p.jobs[0]} title="The last run failed" />}

      {p.edits.length === 0 && !active && (
        <Empty title="No edit yet">Start one from Import with this footage folder.</Empty>
      )}
      {edit.data && <EditView project={p} edit={edit.data} />}
      {edit.isLoading && <Loading />}
    </div>
  )
}

function EditView({ project, edit }: { project: ProjectDetail; edit: EditDetail }) {
  const video = useRef<HTMLVideoElement>(null)
  const [time, setTime] = useState(0)
  const portrait = edit.height > edit.width
  const playing = edit.shots.findIndex((s) => time >= s.start_s && time < s.end_s)
  const list = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const box = list.current
    const row = box?.querySelector<HTMLElement>(`[data-shot="${playing}"]`)
    if (!box || !row || video.current?.paused) return
    if (row.offsetTop < box.scrollTop || row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTo({ top: row.offsetTop - 8, behavior: 'smooth' })
    }
  }, [playing])
  const summary = project.summary as Record<string, unknown> | null
  const moments = (summary?.notable_moments as { segment_id: string; moment: string }[] | undefined) ?? []

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_400px] gap-4">
      <div className="min-w-0 space-y-4">
        <div className={cx('grid gap-4', portrait ? 'grid-cols-[minmax(0,340px)_1fr]' : 'grid-cols-1')}>
          <div className="overflow-hidden rounded-lg bg-black ring-1 ring-white/8">
            {edit.final_url ? (
              <video
                ref={video}
                src={edit.final_url}
                controls
                className={cx('w-full bg-black', portrait ? 'aspect-[9/16]' : 'aspect-video max-h-[62vh]')}
                onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
              />
            ) : (
              <div className={cx('flex items-center justify-center text-zinc-600', portrait ? 'aspect-[9/16]' : 'aspect-video')}>
                Not rendered yet
              </div>
            )}
          </div>
          {portrait && <PlanHeader edit={edit} />}
        </div>
        {!portrait && <PlanHeader edit={edit} />}

        <Panel title={`The director's plan · ${edit.shots.length} shots`} pad={false}>
          <ol ref={list} className="relative max-h-[70vh] divide-y divide-white/5 overflow-y-auto">
            {edit.shots.map((s) => (
              <li
                key={s.index}
                data-shot={s.index}
                className={cx('grid cursor-pointer grid-cols-[28px_150px_1fr] gap-3 px-3.5 py-2.5 hover:bg-white/3', playing === s.index && 'bg-accent/6')}
                onClick={() => {
                  if (video.current) {
                    video.current.currentTime = s.start_s + 0.01
                    video.current.play()
                  }
                }}
              >
                <div className={cx('pt-0.5 text-right font-mono text-[12px]', playing === s.index ? 'text-accent' : 'text-zinc-500')}>{s.index + 1}</div>
                <img src={`${s.thumb_url}?w=320`} loading="lazy" alt="" className="aspect-video w-[150px] rounded object-cover ring-1 ring-white/8" />
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-[12px] text-zinc-300">
                      {clock(s.start_s)} → {clock(s.end_s)}
                    </span>
                    <span className="text-[11.5px] text-zinc-500">{(s.end_s - s.start_s).toFixed(1)} s</span>
                    {s.role && <Badge tone={s.role === 'peak' ? 'accent' : 'default'}>{s.role}</Badge>}
                    {s.starred && (
                      <Badge tone="accent">
                        <Star className="size-3 fill-current" /> starred
                      </Badge>
                    )}
                    {s.transition === 'crossfade' && <Badge tone="info">crossfade</Badge>}
                    {s.speed !== 1 && <Badge tone="info">{s.speed}×</Badge>}
                    <span className="ml-auto font-mono text-[11px] text-zinc-500">
                      {s.segment_id} · {s.clip}@{clock(s.source_in_s)} · {s.score?.toFixed(1)}
                    </span>
                  </div>
                  <p className="text-[12.5px] leading-snug text-zinc-200">{s.reason}</p>
                  {s.description && <p className="truncate text-[11.5px] text-zinc-500">{s.description}</p>}
                </div>
              </li>
            ))}
          </ol>
        </Panel>
      </div>

      <div className="space-y-4">
        {summary && (
          <Panel title="Session">
            <div className="space-y-2.5">
              <div className="text-[14px] font-semibold text-zinc-100">{String(summary.headline ?? '')}</div>
              <SummaryLine label="Where" text={summary.where} />
              <SummaryLine label="Flights" text={summary.flights} />
              <SummaryLine label="Mood" text={summary.mood} />
              <SummaryLine label="Story" text={summary.suggested_story} />
              {moments.length > 0 && (
                <ul className="space-y-1 border-t border-white/6 pt-2.5">
                  {moments.map((m) => (
                    <li key={m.segment_id} className="text-[12px] text-zinc-300">
                      <span className="mr-1.5 font-mono text-[11px] text-zinc-500">{m.segment_id}</span>
                      {m.moment}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Panel>
        )}
        {project.music && (
          <Panel
            title="Music"
            actions={<span className="text-[11.5px] text-zinc-500">{project.music.tempo.toFixed(1)} BPM · {clockShort(project.music.duration_s)}</span>}
          >
            <div className="mb-2 truncate text-[12px] text-zinc-300" title={project.music.path}>
              {project.music.name}
            </div>
            <MusicStrip music={project.music} start={edit.music_start_s} end={edit.music_end_s} height={56} />
            <div className="mt-1 text-[11.5px] text-zinc-500">
              This edit uses {clock(edit.music_start_s)} to {clock(edit.music_end_s)}.
            </div>
            <table className="mt-2 w-full text-[11.5px] tabular-nums">
              <tbody>
                {project.music.sections.map((s) => {
                  const inside = s.end_s > edit.music_start_s && s.start_s < edit.music_end_s
                  return (
                    <tr key={s.index} className={cx('border-t border-white/5', !inside && 'opacity-40')}>
                      <td className="py-0.5 font-mono text-zinc-400">{clock(s.start_s)}</td>
                      <td>
                        <Badge tone={s.label === 'drop' ? 'accent' : 'default'}>{s.label}</Badge>
                      </td>
                      <td className="w-20">
                        <div className="h-1 rounded-full bg-white/6">
                          <div className="h-full rounded-full bg-zinc-300/60" style={{ width: `${s.energy * 100}%` }} />
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Panel>
        )}
        {edit.contact_sheet_url && (
          <Panel title="Contact sheet" pad={false}>
            <a href={edit.contact_sheet_url} target="_blank" rel="noreferrer">
              <img src={edit.contact_sheet_url} alt="Contact sheet" className="w-full rounded-b-lg" />
            </a>
          </Panel>
        )}
      </div>
    </div>
  )
}

function PlanHeader({ edit }: { edit: EditDetail }) {
  const rounds = edit.validation_rounds.filter((r) => r.length > 0).length
  return (
    <Panel title="Plan">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[14px] font-semibold text-zinc-100">{edit.title || edit.name}</span>
          <Badge>
            {edit.width}×{edit.height}
          </Badge>
          <Badge>{clockShort(edit.duration_s)}</Badge>
          <Badge tone={edit.source.startsWith('claude') ? 'good' : 'warn'} title="Who planned this edit">
            {edit.source}
          </Badge>
          {rounds > 0 && <Badge tone="warn">{rounds} repair round{rounds > 1 ? 's' : ''}</Badge>}
          {edit.options?.pace != null && <Badge>pace {String(edit.options.pace)}</Badge>}
          {edit.options?.style != null && <Badge>{String(edit.options.style)}</Badge>}
        </div>
        {edit.concept && <p className="text-[12.5px] leading-relaxed text-zinc-300">{edit.concept}</p>}
        {edit.order_note && <p className="text-[12px] text-zinc-400">{edit.order_note}</p>}
        {edit.closing_note && <p className="text-[12px] text-zinc-400">{edit.closing_note}</p>}
      </div>
    </Panel>
  )
}

function SummaryLine({ label, text }: { label: string; text: unknown }) {
  if (!text) return null
  return (
    <p className="text-[12.5px] leading-snug text-zinc-300">
      <span className="mr-1.5 text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">{label}</span>
      {String(text)}
    </p>
  )
}
