import { useQuery } from '@tanstack/react-query'
import { ArrowUp, File, Folder, FolderSearch, Music, Video } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api, unwrap } from '../api/client'
import { cx } from '../lib/format'
import { TextInput } from './ui'

/** A path field with a small server-side browser: folders to walk, and the files the field wants. */
export function PathInput({
  value,
  onChange,
  want,
  placeholder,
  invalid,
}: {
  value: string
  onChange: (path: string) => void
  want: 'dir' | 'audio'
  placeholder?: string
  invalid?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [dir, setDir] = useState<string>('')
  const box = useRef<HTMLDivElement>(null)
  const listing = useQuery({
    queryKey: ['fs', dir],
    queryFn: () => unwrap(api.GET('/api/fs', { params: { query: { path: dir || '~' } } })),
    enabled: open,
  })
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const browse = () => {
    setDir(value ? (want === 'dir' ? value : value.replace(/\/[^/]*$/, '') || '/') : '~')
    setOpen(!open)
  }
  const entries = (listing.data?.entries ?? []).filter((e) => e.kind === 'dir' || (want === 'audio' && e.kind === 'audio') || (want === 'dir' && e.kind === 'video'))

  return (
    <div ref={box} className="relative">
      <div className="flex gap-1.5">
        <TextInput value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} invalid={invalid} spellCheck={false} />
        <button
          type="button"
          onClick={browse}
          title="Browse"
          className="flex h-8 w-9 shrink-0 items-center justify-center rounded-md bg-ink-800 text-zinc-300 ring-1 ring-white/8 hover:bg-ink-700"
        >
          <FolderSearch className="size-4" />
        </button>
      </div>
      {open && (
        <div className="absolute inset-x-0 top-9 z-20 overflow-hidden rounded-md bg-ink-850 shadow-2xl ring-1 ring-white/10">
          <div className="flex items-center gap-2 border-b border-white/6 px-2 py-1.5">
            <button
              type="button"
              className="rounded p-1 text-zinc-400 hover:bg-white/6 disabled:opacity-30"
              disabled={!listing.data?.parent}
              onClick={() => listing.data?.parent && setDir(listing.data.parent)}
            >
              <ArrowUp className="size-3.5" />
            </button>
            <span className="truncate font-mono text-[11.5px] text-zinc-400">{listing.data?.path ?? dir}</span>
            {want === 'dir' && listing.data && (
              <button
                type="button"
                className="ml-auto shrink-0 rounded bg-accent px-2 py-0.5 text-[11px] font-semibold text-ink-950"
                onClick={() => {
                  onChange(listing.data!.path)
                  setOpen(false)
                }}
              >
                Use this folder
              </button>
            )}
          </div>
          <ul className="max-h-64 overflow-y-auto py-1">
            {entries.length === 0 && <li className="px-3 py-2 text-zinc-500">{listing.isLoading ? 'Loading…' : 'Nothing here'}</li>}
            {entries.map((e) => {
              const Icon = e.kind === 'dir' ? Folder : e.kind === 'audio' ? Music : e.kind === 'video' ? Video : File
              return (
                <li key={e.path}>
                  <button
                    type="button"
                    disabled={want === 'dir' && e.kind === 'video'}
                    onClick={() => {
                      if (e.kind === 'dir') setDir(e.path)
                      else {
                        onChange(e.path)
                        setOpen(false)
                      }
                    }}
                    className={cx(
                      'flex w-full items-center gap-2 px-3 py-1 text-left text-[12.5px]',
                      e.kind === 'video' ? 'text-zinc-500' : 'text-zinc-200 hover:bg-white/6',
                    )}
                  >
                    <Icon className={cx('size-3.5 shrink-0', e.kind === 'dir' ? 'text-accent/80' : 'text-zinc-500')} />
                    <span className="truncate">{e.name}</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}
