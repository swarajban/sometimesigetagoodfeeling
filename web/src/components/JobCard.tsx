import { Check, ChevronDown, ChevronUp, CircleAlert, CircleDollarSign, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { Job } from '../api/client'
import { useJobActions, useJobStream } from '../api/hooks'
import { cx, tokens, usd } from '../lib/format'
import { Badge, Button, ErrorNote, Spinner } from './ui'

const STAGES = [
  ['ingest', 'Proxies'],
  ['segment', 'Segments'],
  ['music', 'Music'],
  ['estimate', 'Estimate'],
  ['score', 'Scoring'],
  ['understand', 'Summary'],
  ['moments', 'Moments'],
  ['direct', 'Director'],
  ['render', 'Render'],
] as const

const STATUS: Record<Job['status'], { label: string; tone: 'default' | 'accent' | 'good' | 'bad' | 'warn' | 'info' }> = {
  queued: { label: 'Queued', tone: 'default' },
  running: { label: 'Running', tone: 'info' },
  approval: { label: 'Waiting for you', tone: 'accent' },
  done: { label: 'Done', tone: 'good' },
  failed: { label: 'Failed', tone: 'bad' },
  cancelled: { label: 'Cancelled', tone: 'default' },
}

export function JobCard({ job: initial, title }: { job: Job; title?: string }) {
  const { job, lines } = useJobStream(initial)
  const { approve, cancel } = useJobActions()
  const [open, setOpen] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [lines.length, open])
  if (!job) return null
  const current = STAGES.findIndex(([key]) => key === job.stage)
  const finished = job.status === 'done'
  const active = job.status === 'running' || job.status === 'queued'
  const status = STATUS[job.status]
  const shown = open ? lines : lines.slice(-4)
  const fraction = job.step_total ? (job.step_done ?? 0) / job.step_total : null

  return (
    <section
      className={cx(
        'overflow-hidden rounded-lg ring-1',
        job.status === 'approval' ? 'bg-ink-900 ring-accent/40' : 'bg-ink-900 ring-white/8',
      )}
    >
      <header className="flex items-center gap-3 px-4 pt-3">
        {active && <Spinner className="text-sky-300" />}
        <div className="text-[13.5px] font-semibold text-zinc-100">
          {title ?? (job.edit && job.edit !== 'default' ? `Making the "${job.edit}" edit` : 'Making the edit')}
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
        {job.attempts > 1 && <Badge title="The job resumed after a restart or a worker failure">attempt {job.attempts}</Badge>}
        <div className="ml-auto flex items-center gap-3 text-[12px] text-zinc-400">
          <span title="Claude spend by this job so far">Claude {usd(job.cost_usd, 3)}</span>
          {(active || job.status === 'approval') && (
            <Button size="sm" tone="ghost" onClick={() => cancel.mutate(job.id)} busy={cancel.isPending}>
              <X className="size-3.5" /> Cancel
            </Button>
          )}
        </div>
      </header>

      <ol className="mt-3 grid grid-cols-9 gap-1 px-4">
        {STAGES.map(([key, label], i) => {
          const done = finished || current > i
          const now = i === current && !finished
          return (
            <li key={key} className="min-w-0">
              <div className="relative h-1 overflow-hidden rounded-full bg-white/8">
                <div
                  className={cx(
                    'absolute inset-y-0 left-0 rounded-full transition-all',
                    done && 'w-full bg-accent/70',
                    now && job.status === 'failed' && 'w-full bg-red-400/70',
                    now && job.status !== 'failed' && 'bg-accent',
                  )}
                  style={now && job.status !== 'failed' ? { width: `${fraction !== null ? Math.max(8, fraction * 100) : 40}%` } : undefined}
                />
              </div>
              <div className={cx('mt-1.5 truncate text-[11px]', now ? 'text-zinc-100' : done ? 'text-zinc-400' : 'text-zinc-600')}>
                {label}
                {now && job.step_total ? ` ${job.step_done}/${job.step_total}` : ''}
              </div>
            </li>
          )
        })}
      </ol>

      {job.status === 'approval' && job.estimate && (
        <div className="mx-4 mt-3 rounded-md bg-accent/6 p-3 ring-1 ring-accent/25">
          <div className="flex items-center gap-2 text-[13px] font-medium text-zinc-100">
            <CircleDollarSign className="size-4 text-accent" />
            Nothing has been sent to Claude yet. Approve the estimate to continue.
          </div>
          <table className="mt-2.5 w-full text-[12px]">
            <thead className="text-left text-zinc-500">
              <tr>
                <th className="py-1 font-medium">Step</th>
                <th className="font-medium">Model</th>
                <th className="text-right font-medium">Requests</th>
                <th className="text-right font-medium">Tokens in / out</th>
                <th className="text-right font-medium">Cost</th>
              </tr>
            </thead>
            <tbody className="text-zinc-300 tabular-nums">
              {job.estimate.steps.filter((s) => s.requests > 0).map((s) => (
                <tr key={s.step} className="border-t border-white/5">
                  <td className="py-1">{s.step}</td>
                  <td className="text-zinc-400">{s.model}</td>
                  <td className="text-right">{s.requests}</td>
                  <td className="text-right text-zinc-400">
                    {tokens(s.input_tokens)} / {tokens(s.output_tokens)}
                  </td>
                  <td className="text-right">{usd(s.cost, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex items-center gap-3">
            <Button tone="primary" onClick={() => approve.mutate(job.id)} busy={approve.isPending}>
              <Check className="size-4" /> Spend about {usd(job.estimate.expected)}
            </Button>
            <span className="text-[12px] text-zinc-400">
              Up to {usd(job.estimate.worst)} if the director needs every repair round. Your limit is{' '}
              {usd(job.estimate.limit)}.
            </span>
          </div>
          <ErrorNote error={approve.error} />
        </div>
      )}

      {job.status === 'failed' && job.error && (
        <div className="mx-4 mt-3 flex items-start gap-2 rounded-md bg-red-500/8 px-3 py-2 text-[12.5px] text-red-200 ring-1 ring-red-400/20">
          <CircleAlert className="mt-0.5 size-4 shrink-0" /> {job.error}
        </div>
      )}

      <div className="mt-3 border-t border-white/6 bg-ink-950/60">
        <div ref={logRef} className={cx('overflow-y-auto px-4 py-2 font-mono text-[11.5px] leading-5 text-zinc-400', open ? 'max-h-72' : 'max-h-24')}>
          {shown.length === 0 && <div className="text-zinc-600">Waiting for the worker…</div>}
          {shown.map((line) => (
            <div key={line.id} className={cx('whitespace-pre-wrap', !line.message.startsWith(' ') && 'text-zinc-200')}>
              {line.message}
            </div>
          ))}
        </div>
        {lines.length > 4 && (
          <button
            onClick={() => setOpen(!open)}
            className="flex w-full items-center justify-center gap-1 border-t border-white/5 py-1 text-[11px] text-zinc-500 hover:text-zinc-300"
          >
            {open ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />}
            {open ? 'Less' : `Full log (${lines.length} lines)`}
          </button>
        )}
      </div>
    </section>
  )
}
