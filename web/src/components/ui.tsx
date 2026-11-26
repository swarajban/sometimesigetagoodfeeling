import { Loader2 } from 'lucide-react'
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'
import { cx } from '../lib/format'

type Tone = 'default' | 'primary' | 'ghost' | 'danger'

export function Button({
  tone = 'default',
  size = 'md',
  busy,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone; size?: 'sm' | 'md'; busy?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || busy}
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-45 focus-visible:outline-2 focus-visible:outline-accent/70',
        size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-[13px]',
        tone === 'primary' && 'bg-accent text-ink-950 hover:bg-[#ffc266]',
        tone === 'default' && 'bg-ink-800 text-zinc-200 ring-1 ring-white/8 hover:bg-ink-700',
        tone === 'ghost' && 'text-zinc-400 hover:bg-white/5 hover:text-zinc-100',
        tone === 'danger' && 'bg-red-500/10 text-red-300 ring-1 ring-red-400/25 hover:bg-red-500/20',
        className,
      )}
    >
      {busy && <Loader2 className="size-3.5 animate-spin" />}
      {children}
    </button>
  )
}

export function Panel({
  title,
  actions,
  children,
  className,
  pad = true,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  pad?: boolean
}) {
  return (
    <section className={cx('rounded-lg bg-ink-900 ring-1 ring-white/6', className)}>
      {(title || actions) && (
        <header className="flex h-10 items-center justify-between gap-3 border-b border-white/6 px-3.5">
          <h2 className="text-[11px] font-semibold tracking-wider text-zinc-400 uppercase">{title}</h2>
          <div className="flex items-center gap-1.5">{actions}</div>
        </header>
      )}
      <div className={pad ? 'p-3.5' : ''}>{children}</div>
    </section>
  )
}

export function Badge({
  children,
  tone = 'default',
  className,
  title,
}: {
  children: ReactNode
  tone?: 'default' | 'accent' | 'good' | 'bad' | 'warn' | 'info'
  className?: string
  title?: string
}) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex h-5 items-center gap-1 rounded px-1.5 text-[11px] font-medium whitespace-nowrap',
        tone === 'default' && 'bg-white/6 text-zinc-300',
        tone === 'accent' && 'bg-accent-soft text-accent',
        tone === 'good' && 'bg-emerald-400/12 text-emerald-300',
        tone === 'bad' && 'bg-red-400/12 text-red-300',
        tone === 'warn' && 'bg-amber-300/12 text-amber-200',
        tone === 'info' && 'bg-sky-400/12 text-sky-300',
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  size = 'md',
}: {
  value: T
  options: { value: T; label: ReactNode; title?: string }[]
  onChange: (value: T) => void
  size?: 'sm' | 'md'
}) {
  return (
    <div className="inline-flex w-fit rounded-md bg-ink-950 p-0.5 ring-1 ring-white/8">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cx(
            'rounded-[5px] font-medium transition-colors',
            size === 'sm' ? 'h-6 px-2 text-[11px]' : 'h-7 px-3 text-[12.5px]',
            value === o.value ? 'bg-ink-700 text-zinc-50 shadow-sm' : 'text-zinc-400 hover:text-zinc-200',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block space-y-1.5">
      <span className="block text-[12px] font-medium text-zinc-300">{label}</span>
      {children}
      {error ? (
        <span className="block text-[11.5px] text-red-300">{error}</span>
      ) : (
        hint && <span className="block text-[11.5px] text-zinc-500">{hint}</span>
      )}
    </label>
  )
}

export function TextInput({ className, invalid, ...rest }: InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }) {
  return (
    <input
      {...rest}
      className={cx(
        'h-8 w-full rounded-md bg-ink-950 px-2.5 text-[13px] text-zinc-100 ring-1 placeholder:text-zinc-600',
        'outline-none focus:ring-accent/60',
        invalid ? 'ring-red-400/50' : 'ring-white/10',
        className,
      )}
    />
  )
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cx('size-4 animate-spin text-zinc-500', className)} />
}

export function Empty({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-white/10 px-6 py-14 text-center">
      <div className="text-[14px] font-medium text-zinc-200">{title}</div>
      {children && <div className="max-w-md text-zinc-500">{children}</div>}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <div className="rounded-md bg-red-500/10 px-3 py-2 text-[12.5px] text-red-200 ring-1 ring-red-400/20">
      {error instanceof Error ? error.message : String(error)}
    </div>
  )
}

export function Loading() {
  return (
    <div className="flex h-40 items-center justify-center">
      <Spinner />
    </div>
  )
}
