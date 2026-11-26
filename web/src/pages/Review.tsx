import { Ban, Check, RefreshCw, Star, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router'
import type { Segment } from '../api/client'
import { useProject, useReplan, useSegments, useSetChoice } from '../api/hooks'
import { JobCard } from '../components/JobCard'
import { Badge, Button, ErrorNote, Loading, Segmented } from '../components/ui'
import { clock, cx } from '../lib/format'

type Filter = 'all' | 'available' | 'used' | 'rejected' | 'starred'

export function Review() {
  const { slug = '' } = useParams()
  const project = useProject(slug)
  const segments = useSegments(slug)
  const setChoice = useSetChoice(slug)
  const replan = useReplan(slug)
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<'time' | 'score'>('time')
  const edits = project.data?.edits ?? []
  const [edit, setEdit] = useState<string | null>(null)
  const currentEdit = edit ?? edits[0]?.name ?? null

  const list = useMemo(() => {
    let rows = segments.data ?? []
    if (filter === 'available') rows = rows.filter((s) => s.available)
    if (filter === 'used') rows = rows.filter((s) => currentEdit && currentEdit in s.used_in)
    if (filter === 'rejected') rows = rows.filter((s) => !s.available)
    if (filter === 'starred') rows = rows.filter((s) => s.choice.star)
    if (sort === 'score') rows = [...rows].sort((a, b) => b.score - a.score)
    return rows
  }, [segments.data, filter, sort, currentEdit])

  const all = segments.data ?? []
  const counts = {
    all: all.length,
    available: all.filter((s) => s.available).length,
    used: all.filter((s) => currentEdit && currentEdit in s.used_in).length,
    rejected: all.filter((s) => !s.available).length,
    starred: all.filter((s) => s.choice.star).length,
  }
  const active = project.data?.active_job

  return (
    <div className="mx-auto max-w-[1800px] space-y-4 px-6 py-5">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[18px] font-semibold text-zinc-50">Review</h1>
          <p className="text-zinc-500">Hover to preview. Excluded segments are never used; starred ones must be. Re-plan to apply.</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Segmented
            value={filter}
            onChange={setFilter}
            options={(['all', 'available', 'used', 'rejected', 'starred'] as Filter[]).map((f) => ({
              value: f,
              label: (
                <span>
                  {f === 'used' ? 'In edit' : f === 'rejected' ? 'Left out' : f[0].toUpperCase() + f.slice(1)}{' '}
                  <span className="text-zinc-500">{counts[f]}</span>
                </span>
              ),
            }))}
          />
          <Segmented
            value={sort}
            onChange={setSort}
            options={[
              { value: 'time', label: 'Time' },
              { value: 'score', label: 'Score' },
            ]}
          />
          {edits.length > 1 && currentEdit && (
            <Segmented value={currentEdit} onChange={setEdit} options={edits.map((e) => ({ value: e.name, label: e.name }))} />
          )}
          {currentEdit && (
            <Button tone="primary" onClick={() => replan.mutate(currentEdit)} busy={replan.isPending} disabled={!!active}>
              <RefreshCw className="size-4" /> Re-plan {currentEdit !== 'default' ? `"${currentEdit}"` : ''}
            </Button>
          )}
        </div>
      </div>
      <ErrorNote error={replan.error ?? setChoice.error} />
      {active && <JobCard key={active.id} job={active} />}
      {segments.isLoading && <Loading />}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
        {list.map((s) => (
          <SegmentCard key={s.id} slug={slug} seg={s} edit={currentEdit} onChoice={(update) => setChoice.mutate({ id: s.id, update })} />
        ))}
      </div>
    </div>
  )
}

function SegmentCard({
  seg,
  edit,
  onChoice,
}: {
  slug: string
  seg: Segment
  edit: string | null
  onChoice: (update: { include?: boolean | null; star?: boolean; taste?: -1 | 0 | 1; reset_include?: boolean }) => void
}) {
  const [hover, setHover] = useState(false)
  const video = useRef<HTMLVideoElement>(null)
  const shot = edit ? seg.used_in[edit] : undefined
  const excluded = seg.choice.include === false
  const forced = seg.choice.include === true
  const includeValue = excluded ? 'out' : forced ? 'in' : 'auto'

  return (
    <article
      className={cx(
        'overflow-hidden rounded-lg bg-ink-900 ring-1 transition',
        seg.choice.star ? 'ring-accent/50' : 'ring-white/6',
        !seg.available && 'opacity-60 hover:opacity-100',
      )}
    >
      <div className="relative aspect-video bg-black" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
        <img src={`${seg.thumb_url}?w=480`} loading="lazy" alt="" className="absolute inset-0 size-full object-cover" />
        {hover && (
          <video
            ref={video}
            src={`${seg.proxy_url}#t=${seg.start_s.toFixed(3)}`}
            muted
            autoPlay
            playsInline
            preload="auto"
            className="absolute inset-0 size-full object-cover"
            onLoadedMetadata={(e) => {
              e.currentTarget.currentTime = seg.start_s
            }}
            onTimeUpdate={(e) => {
              if (e.currentTarget.currentTime >= seg.end_s || e.currentTarget.currentTime < seg.start_s - 0.5) {
                e.currentTarget.currentTime = seg.start_s
              }
            }}
          />
        )}
        <div className="absolute top-2 left-2 flex gap-1">
          <span
            className={cx(
              'rounded px-1.5 py-0.5 font-mono text-[13px] font-semibold backdrop-blur',
              seg.score >= 7 ? 'bg-accent/90 text-ink-950' : seg.score >= 5 ? 'bg-black/60 text-zinc-100' : 'bg-black/60 text-zinc-400',
            )}
            title={`Blended ${seg.score.toFixed(2)} · Claude ${seg.claude_score ?? '–'} · local ${seg.local_score.toFixed(1)}`}
          >
            {seg.score.toFixed(1)}
          </span>
          {shot !== undefined && <span className="rounded bg-emerald-500/85 px-1.5 py-0.5 text-[11px] font-semibold text-ink-950">shot {shot + 1}</span>}
        </div>
        <div className="absolute top-2 right-2 flex gap-1">
          {seg.flags.map((f) => (
            <span key={f} className="rounded bg-red-500/80 px-1.5 py-0.5 text-[10.5px] font-semibold text-white">
              {f}
            </span>
          ))}
        </div>
        <div className="absolute right-2 bottom-2 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200">
          {seg.duration_s.toFixed(1)} s
        </div>
      </div>
      <div className="space-y-2 p-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[12px] font-medium text-zinc-200">{seg.id}</span>
          <span className="font-mono text-[11px] text-zinc-500">
            {seg.clip}@{clock(seg.start_s)}
          </span>
          {seg.kind !== 'flight' && <Badge>{seg.kind}</Badge>}
        </div>
        {seg.tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {seg.tags.slice(0, 6).map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
          </div>
        )}
        {seg.description && <p className="text-[12.5px] leading-snug text-zinc-200">{seg.description}</p>}
        {seg.reason && <p className="text-[12px] leading-snug text-zinc-500 italic">{seg.reason}</p>}
        {!seg.available && (
          <p className="rounded bg-white/4 px-2 py-1 text-[11.5px] text-zinc-400">
            {excluded ? 'Excluded by you: the director never uses it.' : seg.auto_rejected}
          </p>
        )}
        <div className="flex items-center gap-1 border-t border-white/6 pt-2">
          <Segmented
            size="sm"
            value={includeValue}
            onChange={(v) => onChoice(v === 'auto' ? { reset_include: true } : { include: v === 'in' })}
            options={[
              { value: 'auto', label: 'Auto', title: "The director's own pick" },
              { value: 'in', label: <Check className="size-3.5" />, title: 'Include: available to the director, even if left out automatically' },
              { value: 'out', label: <Ban className="size-3.5" />, title: 'Exclude: never used' },
            ]}
          />
          <div className="ml-auto flex items-center gap-0.5">
            <IconToggle on={seg.choice.star} onClick={() => onChoice({ star: !seg.choice.star })} title="Must-have: the edit has to use it" className="text-accent">
              <Star className={cx('size-4', seg.choice.star && 'fill-current')} />
            </IconToggle>
            <IconToggle on={seg.choice.taste === 1} onClick={() => onChoice({ taste: seg.choice.taste === 1 ? 0 : 1 })} title="Like" className="text-emerald-300">
              <ThumbsUp className={cx('size-4', seg.choice.taste === 1 && 'fill-current')} />
            </IconToggle>
            <IconToggle on={seg.choice.taste === -1} onClick={() => onChoice({ taste: seg.choice.taste === -1 ? 0 : -1 })} title="Dislike: the director avoids it unless nothing else fits" className="text-red-300">
              <ThumbsDown className={cx('size-4', seg.choice.taste === -1 && 'fill-current')} />
            </IconToggle>
          </div>
        </div>
      </div>
    </article>
  )
}

function IconToggle({
  on,
  onClick,
  title,
  className,
  children,
}: {
  on: boolean
  onClick: () => void
  title: string
  className: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cx('rounded-md p-1.5 transition-colors', on ? cx('bg-white/8', className) : 'text-zinc-500 hover:bg-white/6 hover:text-zinc-200')}
    >
      {children}
    </button>
  )
}
