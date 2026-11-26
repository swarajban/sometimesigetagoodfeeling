import { KeyRound, Save } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { SettingsOut, SettingsUpdate } from '../api/client'
import { useSaveSettings, useSettings } from '../api/hooks'
import { Badge, Button, ErrorNote, Field, Loading, Panel, Segmented, TextInput } from '../components/ui'

const STEPS = [
  ['score', 'Scoring', 'Looks at every segment'],
  ['understand', 'Session summary', 'One call per session'],
  ['moments', 'Moments', 'Second look at the strongest moves'],
  ['direct', 'Director', 'Plans the edit and repairs it'],
] as const
const WEIGHTS = [
  ['claude', 'Claude score'],
  ['motion', 'Motion'],
  ['sharpness', 'Sharpness'],
  ['exposure', 'Exposure'],
] as const

export function Settings() {
  const settings = useSettings()
  if (settings.isLoading || !settings.data) return <Loading />
  return <SettingsForm initial={settings.data} />
}

function SettingsForm({ initial }: { initial: SettingsOut }) {
  const save = useSaveSettings()
  const [draft, setDraft] = useState(initial)
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  useEffect(() => setDraft(initial), [initial])
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial) || key !== ''
  const paceMode = ['slower', 'normal', 'faster'].includes(draft.pace) ? draft.pace : 'custom'

  const submit = (extra: SettingsUpdate = {}) => {
    save.mutate(
      {
        models: draft.models,
        effort: draft.effort,
        weights: draft.weights,
        style_note: draft.style_note,
        pace: draft.pace,
        max_cost_usd: draft.max_cost_usd,
        ...(key ? { api_key: key } : {}),
        ...extra,
      },
      {
        onSuccess: () => {
          setKey('')
          setSaved(true)
          setTimeout(() => setSaved(false), 2000)
        },
      },
    )
  }

  return (
    <div className="mx-auto max-w-[900px] space-y-4 px-6 py-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-[18px] font-semibold text-zinc-50">Settings</h1>
          <p className="mt-0.5 font-mono text-[11.5px] text-zinc-500">{initial.config_path}</p>
        </div>
        <div className="flex items-center gap-2">
          {saved && <span className="text-[12px] text-emerald-300">Saved</span>}
          <Button tone="primary" disabled={!dirty} busy={save.isPending} onClick={() => submit()}>
            <Save className="size-4" /> Save
          </Button>
        </div>
      </div>
      <ErrorNote error={save.error} />

      <Panel title="Anthropic API key">
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <KeyRound className="size-4 text-zinc-500" />
            {initial.api_key.set ? (
              <>
                <Badge tone="good">Set</Badge>
                <span className="font-mono text-zinc-400">sk-ant-{initial.api_key.hint}</span>
                <span className="text-zinc-500">
                  {initial.api_key.source === 'env'
                    ? 'from ANTHROPIC_API_KEY in the environment, which takes precedence over the file'
                    : 'stored in the settings file'}
                </span>
              </>
            ) : (
              <Badge tone="warn">Not set: runs use local signals and the deterministic director</Badge>
            )}
          </div>
          <div className="flex gap-2">
            <TextInput type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Paste a new key to store it in the settings file" />
            {initial.api_key.source === 'settings' && (
              <Button tone="danger" onClick={() => submit({ api_key: '' })}>
                Remove
              </Button>
            )}
          </div>
        </div>
      </Panel>

      <Panel title="Models">
        <div className="grid grid-cols-[160px_1fr_auto] items-center gap-x-3 gap-y-2.5">
          {STEPS.map(([step, label, hint]) => (
            <div key={step} className="contents">
              <div>
                <div className="text-zinc-200">{label}</div>
                <div className="text-[11px] text-zinc-500">{hint}</div>
              </div>
              <select
                value={draft.models[step]}
                onChange={(e) => setDraft({ ...draft, models: { ...draft.models, [step]: e.target.value } })}
                className="h-8 rounded-md bg-ink-950 px-2 text-[13px] ring-1 ring-white/10 outline-none"
              >
                {[...new Set([draft.models[step], ...initial.available_models])].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </select>
              <Segmented
                size="sm"
                value={draft.effort[step] ?? 'medium'}
                onChange={(v) => setDraft({ ...draft, effort: { ...draft.effort, [step]: v } })}
                options={['low', 'medium', 'high'].map((v) => ({ value: v, label: v }))}
              />
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Scoring weights">
        <div className="space-y-2.5">
          {WEIGHTS.map(([name, label]) => (
            <div key={name} className="grid grid-cols-[160px_1fr_56px] items-center gap-3">
              <span className="text-zinc-300">{label}</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={draft.weights[name] ?? 0}
                onChange={(e) => setDraft({ ...draft, weights: { ...draft.weights, [name]: Number(e.target.value) } })}
              />
              <span className="text-right font-mono text-zinc-400">{(draft.weights[name] ?? 0).toFixed(2)}</span>
            </div>
          ))}
          <p className="text-[11.5px] text-zinc-500">
            How the blended score mixes Claude's view with local signals. Changing these never re-scores; the next run
            re-blends.
          </p>
        </div>
      </Panel>

      <Panel title="Edit defaults">
        <div className="space-y-4">
          <Field label="Pace" hint="How readily the director holds: slower holds the big moments more and keeps fewer connecting shots; faster keeps more short hits. A number is a multiplier.">
            <div className="flex items-center gap-2">
              <Segmented
                value={paceMode}
                onChange={(v) => setDraft({ ...draft, pace: v === 'custom' ? '1.2' : v })}
                options={[
                  { value: 'slower', label: 'Slower' },
                  { value: 'normal', label: 'Normal' },
                  { value: 'faster', label: 'Faster' },
                  { value: 'custom', label: '×' },
                ]}
              />
              {paceMode === 'custom' && <TextInput className="w-20" value={draft.pace} onChange={(e) => setDraft({ ...draft, pace: e.target.value })} />}
            </div>
          </Field>
          <Field label="Style note" hint="Goes into every director prompt.">
            <textarea
              value={draft.style_note}
              onChange={(e) => setDraft({ ...draft, style_note: e.target.value })}
              rows={4}
              placeholder="I like low proximity passes over water; skip high cruising."
              className="w-full rounded-md bg-ink-950 px-2.5 py-2 text-[13px] ring-1 ring-white/10 outline-none placeholder:text-zinc-600 focus:ring-accent/60"
            />
          </Field>
          <Field label="Spend limit per run (USD)" hint="A run whose worst-case estimate is above this doesn't start; it also caps spend during the run.">
            <TextInput
              className="w-28"
              type="number"
              min={0}
              step={0.5}
              value={draft.max_cost_usd}
              onChange={(e) => setDraft({ ...draft, max_cost_usd: Number(e.target.value) })}
            />
          </Field>
        </div>
      </Panel>
      <p className="text-[11.5px] text-zinc-600">Data folder: {initial.data_root}</p>
    </div>
  )
}
