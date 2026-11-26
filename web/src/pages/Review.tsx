import { useQuery } from '@tanstack/react-query'
import { Ban, Check, RefreshCw, Star, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useParams } from 'react-router'
import { api, unwrap, type Choice, type ChoiceUpdate, type Moment, type Segment } from '../api/client'
import { keys, useProject, useReplan, useSegments, useSetChoice } from '../api/hooks'
import { JobCard } from '../components/JobCard'
import { Badge, Button, ErrorNote, Loading, Segmented } from '../components/ui'
import { clock, cx } from '../lib/format'

type Filter = 'all' | 'available' | 'used' | 'rejected' | 'starred'

export function Review() {
  const { slug = '' } = useParams()
  const project = useProject(slug)
  const segments = useSegments(slug)
  const moments = useQuery({
    queryKey: [...keys.segments(slug), 'moments'],
    queryFn: () => unwrap(api.GET('/api/projects/{slug}/moments', { params: { path: { slug } } })),
  })
  const setChoice = useSetChoice(slug)
  const replan = useReplan(slug)
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<'time' | 'score'>('time')
  const edits = project.data?.edits ?? []
  const [edit, setEdit] = useState<string | null>(null)
  const currentEdit = edit ?? edits[0]?.name ?? null

  const keep = (row: { available: boolean; used_in: Record<string, number>; choice: Choice }) =>
    filter === 'all' ||
    (filter === 'available' && row.available) ||
    (filter === 'used' && !!currentEdit && currentEdit in row.used_in) ||
    (filter === 'rejected' && !row.available) ||
    (filter === 'starred' && row.choice.star)

  const list = useMemo(() => {
    const rows = (segments.data ?? []).filter(keep)
    return sort === 'score' ? [...rows].sort((a, b) => b.score - a.score) : rows
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segments.data, filter, sort, currentEdit])
  const momentList = (moments.data ?? []).filter(keep)

  const all = [...(segments.data ?? []), ...(moments.data ?? [])]
  const counts = {
    all: all.length,
    available: all.filter((s) => s.available).length,
    used: all.filter((s) => currentEdit && currentEdit in s.used_in).length,
    rejected: all.filter((s) => !s.available).length,
    starred: all.filter((s) => s.choice.star).length,
  }
  const active = project.data?.active_job
  const choose = (id: string) => (update: ChoiceUpdate) => setChoice.mutate({ id, update })

  return (
    <div className="mx-auto max-w-[1800px] space-y-4 px-6 py-5">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-[18px] font-semibold text-zinc-50">Review</h1>
          <p className="text-zinc-500">Hover to preview. Excluded picks are never used; starred ones must be. Re-plan to apply.</p>
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
      {(segments.isLoading || moments.isLoading) && <Loading />}

      {momentList.length > 0 && (
        <section>
          <h2 className="mb-2 flex items-baseline gap-2 text-[11px] font-semibold tracking-wider text-zinc-400 uppercase">
            Moments · {momentList.length}
            <span className="font-normal tracking-normal text-zinc-500 normal-case">
              Whole moves the director can hold from approach to exit. Excluding a segment inside one keeps the director
              off the whole moment.
            </span>
          </h2>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
            {momentList.map((mo) => (
              <PickCard
                key={mo.id}
                kind="moment"
                pick={mo}
                edit={currentEdit}
                score={mo.worth}
                scoreTitle={`Worth ${mo.worth.toFixed(1)} of 10 for the whole move (a different scale from segment scores)`}
                heading={
                  <>
                    <span className="font-mono text-[12px] font-medium text-accent">{mo.id}</span>
                    <span className="font-mono text-[11px] text-zinc-500">
                      {mo.clip}@{clock(mo.start_s)}–{clock(mo.end_s)}
                    </span>
                    <Badge tone="accent">{mo.kind}</Badge>
                    <Badge title="hold=full plays the move from approach to exit">{mo.hold}</Badge>
                  </>
                }
                tags={mo.segment_ids}
                description={mo.description}
                reason={mo.reason}
                leftOut={
                  mo.choice.include === false
                    ? 'Excluded by you: the director never uses it.'
                    : mo.blocked_by.length
                      ? `Blocked: you excluded ${mo.blocked_by.join(', ')}, which it spans.`
                      : null
                }
                onChoice={choose(mo.id)}
              />
            ))}
          </div>
        </section>
      )}

      <section>
        {momentList.length > 0 && (
          <h2 className="mb-2 text-[11px] font-semibold tracking-wider text-zinc-400 uppercase">Segments · {list.length}</h2>
        )}
        <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
          {list.map((s) => (
            <PickCard
              key={s.id}
              kind="segment"
              pick={s}
              edit={currentEdit}
              score={s.score}
              scoreTitle={`Blended ${s.score.toFixed(2)} · Claude ${s.claude_score ?? '–'} · local ${s.local_score.toFixed(1)}`}
              heading={
                <>
                  <span className="font-mono text-[12px] font-medium text-zinc-200">{s.id}</span>
                  <span className="font-mono text-[11px] text-zinc-500">
                    {s.clip}@{clock(s.start_s)}
                  </span>
                  {s.kind !== 'flight' && <Badge>{s.kind}</Badge>}
                  {s.moments.map((id) => (
                    <Badge key={id} tone="accent" title="Part of this moment">
                      in {id}
                    </Badge>
                  ))}
                </>
              }
              tags={s.tags}
              flags={s.flags}
              description={s.description}
              reason={s.reason}
              leftOut={s.available ? null : s.choice.include === false ? 'Excluded by you: the director never uses it.' : s.auto_rejected}
              onChoice={choose(s.id)}
            />
          ))}
        </div>
      </section>
    </div>
  )
}

function PickCard({
  kind,
  pick,
  edit,
  score,
  scoreTitle,
  heading,
  tags,
  flags = [],
  description,
  reason,
  leftOut,
  onChoice,
}: {
  kind: 'segment' | 'moment'
  pick: Segment | Moment
  edit: string | null
  score: number
  scoreTitle: string
  heading: React.ReactNode
  tags: string[]
  flags?: string[]
  description: string
  reason: string
  leftOut: string | null | undefined
  onChoice: (update: ChoiceUpdate) => void
}) {
  const [hover, setHover] = useState(false)
  const shot = edit ? pick.used_in[edit] : undefined
  const { choice } = pick
  const includeValue = choice.include === false ? 'out' : choice.include === true ? 'in' : 'auto'

  return (
    <article
      className={cx(
        'overflow-hidden rounded-lg bg-ink-900 ring-1 transition',
        choice.star ? 'ring-accent/60' : kind === 'moment' ? 'ring-accent/15' : 'ring-white/6',
        !pick.available && 'opacity-60 hover:opacity-100',
      )}
    >
      <div className="relative aspect-video bg-black" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
        <img src={`${pick.thumb_url}?w=480`} loading="lazy" alt="" className="absolute inset-0 size-full object-cover" />
        {hover && (
          <video
            src={`${pick.proxy_url}#t=${pick.start_s.toFixed(3)}`}
            muted
            autoPlay
            playsInline
            preload="auto"
            className="absolute inset-0 size-full object-cover"
            onLoadedMetadata={(e) => {
              e.currentTarget.currentTime = pick.start_s
            }}
            onTimeUpdate={(e) => {
              const t = e.currentTarget.currentTime
              if (t >= pick.end_s || t < pick.start_s - 0.5) e.currentTarget.currentTime = pick.start_s
            }}
          />
        )}
        <div className="absolute top-2 left-2 flex gap-1">
          <span
            className={cx(
              'rounded px-1.5 py-0.5 font-mono text-[13px] font-semibold backdrop-blur',
              score >= 7 ? 'bg-accent/90 text-ink-950' : score >= 5 ? 'bg-black/60 text-zinc-100' : 'bg-black/60 text-zinc-400',
            )}
            title={scoreTitle}
          >
            {score.toFixed(1)}
          </span>
          {shot !== undefined && <span className="rounded bg-emerald-500/85 px-1.5 py-0.5 text-[11px] font-semibold text-ink-950">shot {shot + 1}</span>}
        </div>
        <div className="absolute top-2 right-2 flex gap-1">
          {flags.map((f) => (
            <span key={f} className="rounded bg-red-500/80 px-1.5 py-0.5 text-[10.5px] font-semibold text-white">
              {f}
            </span>
          ))}
        </div>
        <div className="absolute right-2 bottom-2 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200">
          {pick.duration_s.toFixed(1)} s
        </div>
      </div>
      <div className="space-y-2 p-3">
        <div className="flex flex-wrap items-center gap-2">{heading}</div>
        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {tags.slice(0, 6).map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
          </div>
        )}
        {description && <p className="text-[12.5px] leading-snug text-zinc-200">{description}</p>}
        {reason && <p className="text-[12px] leading-snug text-zinc-500 italic">{reason}</p>}
        {leftOut && <p className="rounded bg-white/4 px-2 py-1 text-[11.5px] text-zinc-400">{leftOut}</p>}
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
            <IconToggle on={choice.star} onClick={() => onChoice({ star: !choice.star })} title="Must-have: the edit has to use it" className="text-accent">
              <Star className={cx('size-4', choice.star && 'fill-current')} />
            </IconToggle>
            <IconToggle on={choice.taste === 1} onClick={() => onChoice({ taste: choice.taste === 1 ? 0 : 1 })} title="Like" className="text-emerald-300">
              <ThumbsUp className={cx('size-4', choice.taste === 1 && 'fill-current')} />
            </IconToggle>
            <IconToggle
              on={choice.taste === -1}
              onClick={() => onChoice({ taste: choice.taste === -1 ? 0 : -1 })}
              title="Dislike: the director avoids it unless nothing else fits"
              className="text-red-300"
            >
              <ThumbsDown className={cx('size-4', choice.taste === -1 && 'fill-current')} />
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
