import { useState } from 'react'
import { useParams } from 'react-router'
import { useAiEntry, useAiLog } from '../api/hooks'
import { Badge, Empty, ErrorNote, Loading, Spinner } from '../components/ui'
import { cx, tokens, usd } from '../lib/format'

type Block = Record<string, unknown>

export function ClaudeLog() {
  const { slug = '' } = useParams()
  const log = useAiLog(slug)
  const [selected, setSelected] = useState<number | null>(null)
  const id = selected ?? log.data?.entries[0]?.id ?? null
  const entry = useAiEntry(slug, id)

  if (log.isLoading) return <Loading />
  if (log.error) return <div className="p-6"><ErrorNote error={log.error} /></div>
  if (!log.data?.entries.length) return <div className="p-6"><Empty title="No Claude calls yet">Every prompt and response for this project shows up here.</Empty></div>

  return (
    <div className="grid h-full grid-cols-[340px_1fr]">
      <aside className="overflow-y-auto border-r border-white/6">
        <div className="sticky top-0 border-b border-white/6 bg-ink-950 px-3.5 py-2.5 text-[12px] text-zinc-400">
          {log.data.total} calls · {usd(log.data.total_cost_usd, 3)}
        </div>
        <ul>
          {log.data.entries.map((e) => (
            <li key={e.id}>
              <button
                onClick={() => setSelected(e.id)}
                className={cx('w-full border-b border-white/4 px-3.5 py-2 text-left hover:bg-white/3', id === e.id && 'bg-white/6')}
              >
                <div className="flex items-center gap-2">
                  <Badge tone={e.step === 'score' ? 'default' : e.step === 'direct' ? 'accent' : e.step === 'repair' ? 'warn' : 'info'}>{e.step}</Badge>
                  <span className="truncate text-[12px] text-zinc-400">{e.model}</span>
                  <span className="ml-auto font-mono text-[11.5px] text-zinc-300">{usd(e.cost_usd, 3)}</span>
                </div>
                <div className="mt-1 flex gap-3 text-[11px] text-zinc-500">
                  <span>#{e.id}</span>
                  <span>{new Date(e.created_at * 1000).toLocaleString()}</span>
                  <span className="ml-auto">
                    {tokens(e.input_tokens)} in · {tokens(e.output_tokens)} out
                  </span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="min-w-0 overflow-y-auto px-6 py-5">
        {entry.isLoading && <Spinner />}
        {entry.data && (
          <div className="space-y-5">
            <div className="flex items-center gap-3">
              <h1 className="text-[16px] font-semibold text-zinc-50">
                {entry.data.step} · #{entry.data.id}
              </h1>
              <span className="text-zinc-400">{entry.data.model}</span>
              <span className="ml-auto text-[12px] text-zinc-400">
                {entry.data.input_tokens} in · {entry.data.output_tokens} out · {entry.data.cache_read_tokens} cache read · {usd(entry.data.cost_usd, 4)}
              </span>
            </div>
            <Section title="System">
              {((entry.data.request.system as Block[] | string | undefined) ?? []) instanceof Array ? (
                (entry.data.request.system as Block[]).map((b, i) => <Pre key={i}>{String(b.text ?? '')}</Pre>)
              ) : (
                <Pre>{String(entry.data.request.system)}</Pre>
              )}
            </Section>
            {((entry.data.request.messages as { role: string; content: string | Block[] }[]) ?? []).map((msg, i) => (
              <Section key={i} title={msg.role === 'user' ? 'Prompt' : 'Earlier reply'}>
                <Content content={msg.content} />
              </Section>
            ))}
            <Section title={`Response · stop: ${String(entry.data.response.stop_reason ?? '')}`} accent>
              <Content content={(entry.data.response.content as Block[]) ?? []} />
            </Section>
          </div>
        )}
      </section>
    </div>
  )
}

function Section({ title, children, accent }: { title: string; children: React.ReactNode; accent?: boolean }) {
  return (
    <div>
      <h2 className={cx('mb-1.5 text-[11px] font-semibold tracking-wider uppercase', accent ? 'text-accent' : 'text-zinc-500')}>{title}</h2>
      <div className="space-y-2">{children}</div>
    </div>
  )
}

function Pre({ children, dim }: { children: React.ReactNode; dim?: boolean }) {
  return (
    <pre className={cx('rounded-md bg-ink-900 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap ring-1 ring-white/6', dim ? 'text-zinc-500' : 'text-zinc-200')}>
      {children}
    </pre>
  )
}

function Content({ content }: { content: string | Block[] }) {
  if (typeof content === 'string') return <Pre>{content}</Pre>
  return (
    <>
      {content.map((b, i) => {
        if (b.type === 'text') return <Pre key={i}>{String(b.text)}</Pre>
        if (b.type === 'image') {
          const src = b.source as { bytes?: number; sha256?: string }
          return (
            <div key={i} className="rounded-md bg-ink-900 px-3 py-2 font-mono text-[11.5px] text-zinc-500 ring-1 ring-white/6">
              [image · {Math.round((src.bytes ?? 0) / 1024)} KB · {src.sha256}]
            </div>
          )
        }
        if (b.type === 'thinking') {
          return (
            <details key={i}>
              <summary className="cursor-pointer text-[12px] text-zinc-500">Thinking</summary>
              <Pre dim>{String(b.thinking ?? '')}</Pre>
            </details>
          )
        }
        if (b.type === 'tool_use') {
          return (
            <div key={i}>
              <div className="mb-1 text-[12px] text-zinc-400">
                Tool call <span className="font-mono text-zinc-200">{String(b.name)}</span>
              </div>
              <Pre>{JSON.stringify(b.input, null, 2)}</Pre>
            </div>
          )
        }
        if (b.type === 'tool_result') {
          return (
            <div key={i}>
              <div className="mb-1 text-[12px] text-zinc-400">Tool result{b.is_error ? ' (error)' : ''}</div>
              <Pre>{typeof b.content === 'string' ? b.content : JSON.stringify(b.content, null, 2)}</Pre>
            </div>
          )
        }
        return <Pre key={i}>{JSON.stringify(b, null, 2)}</Pre>
      })}
    </>
  )
}
