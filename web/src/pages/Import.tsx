import { useMutation, useQuery } from '@tanstack/react-query'
import { ArrowRight, Info } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { api, unwrap, type ImportRequest } from '../api/client'
import { useSettings } from '../api/hooks'
import { MusicStrip } from '../components/MusicStrip'
import { PathInput } from '../components/PathInput'
import { Badge, Button, ErrorNote, Field, Panel, Segmented, Spinner, TextInput } from '../components/ui'
import { clock, cx } from '../lib/format'

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

function parseTime(text: string): number | null {
  const t = text.trim()
  if (!t) return null
  const parts = t.split(':').map(Number)
  if (parts.some((p) => Number.isNaN(p))) return null
  return parts.reduce((acc, p) => acc * 60 + p, 0)
}

function fmt(seconds: number): string {
  const ms = Math.round(seconds * 1000)
  const m = Math.floor(ms / 60000)
  const rest = (ms % 60000) / 1000
  return `${String(m).padStart(2, '0')}:${rest.toFixed(3).padStart(6, '0')}`
}

const LENGTHS = { landscape: [60, 90, 150, 180], reel: [15, 30, 45, 60] }

export function Import() {
  const navigate = useNavigate()
  const settings = useSettings()
  const [footage, setFootage] = useState('')
  const [music, setMusic] = useState('')
  const [project, setProject] = useState('')
  const [format, setFormat] = useState<'landscape' | 'reel'>('landscape')
  const [style, setStyle] = useState<'fast' | 'cinematic'>('fast')
  const [res, setRes] = useState<'1080' | '4k'>('1080')
  const [length, setLength] = useState('150')
  const [lengthTouched, setLengthTouched] = useState(false)
  const [pace, setPace] = useState<string>('')
  const [musicStart, setMusicStart] = useState('')

  useEffect(() => {
    if (!lengthTouched) setLength(format === 'reel' ? '30' : '150')
  }, [format, lengthTouched])
  const paceValue = pace || settings.data?.pace || 'normal'
  const paceMode = ['slower', 'normal', 'faster'].includes(paceValue) ? paceValue : 'custom'

  const check = useQuery({
    queryKey: ['import-check', useDebounced(footage, 350), useDebounced(music, 350), useDebounced(project, 350), format],
    queryFn: ({ queryKey }) =>
      unwrap(
        api.POST('/api/import/check', {
          body: { footage: queryKey[1] as string, music: queryKey[2] as string, project: (queryKey[3] as string) || null, format },
        }),
      ),
    enabled: !!(footage || music),
    placeholderData: (prev) => prev,
  })
  const lengthNum = Number(length) || 0
  const analysis = useQuery({
    queryKey: ['music', check.data?.music.path, useDebounced(lengthNum, 400)],
    queryFn: () => unwrap(api.POST('/api/music/analyze', { body: { path: check.data!.music.path, length: lengthNum || 150 } })),
    enabled: !!check.data?.music.ok && lengthNum >= 4,
    staleTime: Infinity,
    placeholderData: (prev) => prev,
  })

  const auto = useMemo(() => {
    const s = analysis.data?.suggestions ?? []
    return (format === 'reel' ? s.find((x) => x.why.includes('reel')) : undefined) ?? s[0]
  }, [analysis.data, format])
  const startS = parseTime(musicStart)
  const shownStart = startS ?? auto?.start_s ?? null
  const shownEnd = shownStart !== null ? Math.min(analysis.data?.duration_s ?? Infinity, shownStart + lengthNum) : null

  const submit = useMutation({
    mutationFn: (body: ImportRequest) => unwrap(api.POST('/api/import', { body })),
    onSuccess: (data) => navigate(`/p/${data.project}`),
  })
  const ready = check.data?.footage.ok && check.data?.music.ok && lengthNum >= 4 && (!musicStart || startS !== null)

  return (
    <div className="mx-auto grid max-w-[1400px] grid-cols-[minmax(380px,460px)_1fr] gap-5 px-6 py-6">
      <div className="space-y-4">
        <div>
          <h1 className="text-[18px] font-semibold text-zinc-50">Import</h1>
          <p className="mt-0.5 text-zinc-500">Your footage is only read; nothing is written next to it.</p>
        </div>
        <Panel title="Media">
          <div className="space-y-3.5">
            <Field
              label="Footage folder"
              error={footage && check.data && !check.data.footage.ok ? check.data.footage.message : undefined}
              hint={check.data?.footage.ok ? check.data.footage.message : 'The folder with your DJI clips (.mp4, .mov)'}
            >
              <PathInput value={footage} onChange={setFootage} want="dir" placeholder="~/Movies/O4/2026-11-24" invalid={!!footage && check.data?.footage.ok === false} />
            </Field>
            <Field
              label="Music"
              error={music && check.data && !check.data.music.ok ? check.data.music.message : undefined}
              hint={check.data?.music.ok ? check.data.music.message : 'Any track ffmpeg can read'}
            >
              <PathInput value={music} onChange={setMusic} want="audio" placeholder="~/Music/track.mp3" invalid={!!music && check.data?.music.ok === false} />
            </Field>
            <Field
              label="Project name"
              hint={
                check.data?.project_slug
                  ? check.data.project_exists
                    ? `Adds to the existing project "${check.data.project_slug}"; scores already made are reused`
                    : `New project "${check.data.project_slug}"`
                  : 'Defaults to the folder name'
              }
            >
              <TextInput value={project} onChange={(e) => setProject(e.target.value)} placeholder="optional" />
            </Field>
          </div>
        </Panel>

        <Panel title="Edit">
          <div className="grid grid-cols-[110px_1fr] items-center gap-x-3 gap-y-3">
            <span className="text-zinc-400">Format</span>
            <Segmented
              value={format}
              onChange={setFormat}
              options={[
                { value: 'landscape', label: 'Landscape 16:9' },
                { value: 'reel', label: 'Reel 9:16' },
              ]}
            />
            <span className="text-zinc-400">Style</span>
            <Segmented
              value={style}
              onChange={setStyle}
              options={[
                { value: 'fast', label: 'Fast' },
                { value: 'cinematic', label: 'Cinematic' },
              ]}
            />
            <span className="text-zinc-400">Resolution</span>
            <Segmented
              value={res}
              onChange={setRes}
              options={[
                { value: '1080', label: '1080p' },
                { value: '4k', label: '4K' },
              ]}
            />
            <span className="text-zinc-400">Length</span>
            <div className="flex items-center gap-2">
              <TextInput
                className="w-20 tabular-nums"
                value={length}
                onChange={(e) => {
                  setLength(e.target.value.replace(/[^0-9.]/g, ''))
                  setLengthTouched(true)
                }}
                invalid={lengthNum < 4}
              />
              <span className="text-zinc-500">s</span>
              <div className="flex gap-1">
                {LENGTHS[format].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => {
                      setLength(String(n))
                      setLengthTouched(true)
                    }}
                    className={cx('rounded px-1.5 py-0.5 text-[11.5px]', Number(length) === n ? 'bg-white/10 text-zinc-100' : 'text-zinc-500 hover:text-zinc-200')}
                  >
                    {n >= 60 ? `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}` : `${n}s`}
                  </button>
                ))}
              </div>
            </div>
            <span className="text-zinc-400">Pace</span>
            <div className="flex items-center gap-2">
              <Segmented
                value={paceMode}
                onChange={(v) => setPace(v === 'custom' ? '1.2' : v)}
                options={[
                  { value: 'slower', label: 'Slower', title: 'Longer holds' },
                  { value: 'normal', label: 'Normal' },
                  { value: 'faster', label: 'Faster', title: 'Cuts more often' },
                  { value: 'custom', label: '×' },
                ]}
              />
              {paceMode === 'custom' && <TextInput className="w-16" value={paceValue} onChange={(e) => setPace(e.target.value)} />}
            </div>
            <span className="text-zinc-400">Start in song</span>
            <div className="flex items-center gap-2">
              <TextInput
                className="w-28 font-mono"
                value={musicStart}
                placeholder={auto ? auto.start.slice(0, 8) : 'auto'}
                onChange={(e) => setMusicStart(e.target.value)}
                invalid={!!musicStart && startS === null}
              />
              {musicStart ? (
                <Button size="sm" tone="ghost" onClick={() => setMusicStart('')}>
                  Auto
                </Button>
              ) : (
                <span className="text-[11.5px] text-zinc-500">auto · snaps to a downbeat</span>
              )}
            </div>
          </div>
        </Panel>

        <div className="flex items-center gap-3">
          <Button
            tone="primary"
            disabled={!ready}
            busy={submit.isPending}
            onClick={() =>
              submit.mutate({
                footage,
                music,
                project: project || null,
                format,
                style,
                res,
                length: lengthNum,
                pace: pace || null,
                music_start: musicStart || null,
              })
            }
          >
            Analyse footage <ArrowRight className="size-4" />
          </Button>
          <span className="flex items-center gap-1.5 text-[12px] text-zinc-500">
            <Info className="size-3.5" /> Proxies and analysis run locally. You approve the Claude estimate before any call.
          </span>
        </div>
        <ErrorNote error={submit.error} />
      </div>

      <div className="space-y-4">
        <Panel
          title="Music"
          actions={
            analysis.data && (
              <span className="text-[12px] text-zinc-400 tabular-nums">
                {analysis.data.tempo.toFixed(1)} BPM · {clock(analysis.data.duration_s)} · {analysis.data.beats} beats
              </span>
            )
          }
        >
          {!check.data?.music.ok && <div className="py-10 text-center text-zinc-500">Pick a track to see its sections and suggested stretches.</div>}
          {check.data?.music.ok && analysis.isLoading && (
            <div className="flex items-center justify-center gap-2 py-10 text-zinc-400">
              <Spinner /> Finding beats and sections…
            </div>
          )}
          <ErrorNote error={analysis.error} />
          {analysis.data && check.data?.music.ok && (
            <div className="space-y-4">
              <MusicStrip
                music={analysis.data}
                start={shownStart}
                end={shownEnd}
                height={96}
                onPick={(s) => setMusicStart(fmt(s))}
              />
              <div className="text-[12px] text-zinc-500">
                Click the track to start there. The highlighted stretch is what the edit uses ({clock(shownStart)} to{' '}
                {clock(shownEnd)}){!musicStart && ', picked automatically'}.
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <h3 className="mb-1.5 text-[11px] font-semibold tracking-wider text-zinc-500 uppercase">Suggested stretches</h3>
                  <ul className="space-y-1">
                    {analysis.data.suggestions.map((s) => (
                      <li key={s.start_s}>
                        <button
                          type="button"
                          onClick={() => setMusicStart(s.start)}
                          className={cx(
                            'flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-white/5',
                            Math.abs((shownStart ?? -1) - s.start_s) < 0.05 && 'bg-accent/8 ring-1 ring-accent/30',
                          )}
                        >
                          <span className="font-mono text-[12px] text-zinc-100">{s.start.slice(0, 8)}</span>
                          <span className="text-[12px] text-zinc-400">{s.why}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h3 className="mb-1.5 text-[11px] font-semibold tracking-wider text-zinc-500 uppercase">Sections</h3>
                  <table className="w-full text-[12px] tabular-nums">
                    <tbody>
                      {analysis.data.sections.map((s) => (
                        <tr key={s.index} className="cursor-pointer border-t border-white/5 hover:bg-white/4" onClick={() => setMusicStart(fmt(s.start_s))}>
                          <td className="py-1 pl-1 text-zinc-500">{s.index}</td>
                          <td className="font-mono text-zinc-300">{clock(s.start_s)}</td>
                          <td className="font-mono text-zinc-500">{clock(s.end_s)}</td>
                          <td>
                            <Badge tone={s.label === 'drop' ? 'accent' : 'default'}>{s.label}</Badge>
                          </td>
                          <td className="w-24">
                            <div className="h-1.5 rounded-full bg-white/6">
                              <div className="h-full rounded-full bg-zinc-300/60" style={{ width: `${s.energy * 100}%` }} />
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="mt-1.5 text-[11px] text-zinc-600">Labels and downbeats are estimates.</p>
                </div>
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
