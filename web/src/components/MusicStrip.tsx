import type { MusicInfo } from '../api/client'
import { clock, cx } from '../lib/format'

const LABEL_COLOR: Record<string, string> = {
  intro: 'bg-sky-400/10',
  build: 'bg-violet-400/12',
  drop: 'bg-accent/18',
  main: 'bg-emerald-400/8',
  break: 'bg-zinc-400/8',
  outro: 'bg-sky-400/8',
}

/** The track's energy across its length, its sections, and the stretch the edit uses. Click to pick a start. */
export function MusicStrip({
  music,
  start,
  end,
  onPick,
  height = 72,
}: {
  music: MusicInfo
  start: number | null
  end: number | null
  onPick?: (seconds: number) => void
  height?: number
}) {
  const d = music.duration_s
  const pct = (s: number) => `${(Math.max(0, Math.min(d, s)) / d) * 100}%`
  const max = Math.max(...music.energy, 0.001)
  return (
    <div>
      <div
        className={cx('relative overflow-hidden rounded-md bg-ink-950 ring-1 ring-white/8', onPick && 'cursor-crosshair')}
        style={{ height }}
        onClick={(e) => {
          if (!onPick) return
          const rect = e.currentTarget.getBoundingClientRect()
          onPick(((e.clientX - rect.left) / rect.width) * d)
        }}
      >
        {music.sections.map((s) => (
          <div
            key={s.index}
            className={cx('absolute inset-y-0 border-l border-white/8', LABEL_COLOR[s.label] ?? 'bg-white/4')}
            style={{ left: pct(s.start_s), width: `calc(${pct(s.end_s)} - ${pct(s.start_s)})` }}
            title={`${s.label} ${clock(s.start_s)}–${clock(s.end_s)} · energy ${s.energy.toFixed(2)}`}
          />
        ))}
        <div className="absolute inset-0 flex items-end gap-px px-px">
          {music.energy.map((e, i) => (
            <div key={i} className="flex-1 rounded-t-[1px] bg-zinc-300/35" style={{ height: `${6 + (e / max) * 80}%` }} />
          ))}
        </div>
        {start !== null && end !== null && (
          <div
            className="pointer-events-none absolute inset-y-0 border-x-2 border-accent bg-accent/12"
            style={{ left: pct(start), width: `calc(${pct(end)} - ${pct(start)})` }}
          />
        )}
      </div>
      <div className="relative mt-1 h-4 text-[10.5px] text-zinc-500">
        {music.sections.map((s) => (
          <span key={s.index} className="absolute truncate pl-1" style={{ left: pct(s.start_s), width: `calc(${pct(s.end_s)} - ${pct(s.start_s)})` }}>
            {s.label}
          </span>
        ))}
      </div>
    </div>
  )
}
