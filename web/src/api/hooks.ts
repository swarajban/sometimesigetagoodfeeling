import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { api, unwrap, type ChoiceUpdate, type Job, type JobEvent, type SettingsUpdate } from './client'

export const keys = {
  projects: ['projects'] as const,
  project: (slug: string) => ['project', slug] as const,
  edit: (slug: string, edit: string) => ['edit', slug, edit] as const,
  segments: (slug: string) => ['segments', slug] as const,
  settings: ['settings'] as const,
  aiLog: (slug: string) => ['ai-log', slug] as const,
  aiEntry: (slug: string, id: number) => ['ai-log', slug, id] as const,
}

export function useProjects() {
  return useQuery({
    queryKey: keys.projects,
    queryFn: () => unwrap(api.GET('/api/projects')),
    refetchInterval: (q) => (q.state.data?.some((p) => p.active_job) ? 3000 : false),
  })
}

export function useProject(slug: string) {
  return useQuery({
    queryKey: keys.project(slug),
    queryFn: () => unwrap(api.GET('/api/projects/{slug}', { params: { path: { slug } } })),
  })
}

export function useEdit(slug: string, edit: string | undefined) {
  return useQuery({
    queryKey: keys.edit(slug, edit ?? ''),
    queryFn: () => unwrap(api.GET('/api/projects/{slug}/edits/{edit}', { params: { path: { slug, edit: edit! } } })),
    enabled: !!edit,
  })
}

export function useSegments(slug: string) {
  return useQuery({
    queryKey: keys.segments(slug),
    queryFn: () => unwrap(api.GET('/api/projects/{slug}/segments', { params: { path: { slug } } })),
  })
}

export function useSetChoice(slug: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, update }: { id: string; update: ChoiceUpdate }) =>
      unwrap(
        api.PUT('/api/projects/{slug}/segments/{seg_id}/choice', {
          params: { path: { slug, seg_id: id } },
          body: update,
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.segments(slug) }),
  })
}

export function useReplan(slug: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (edit: string) =>
      unwrap(api.POST('/api/projects/{slug}/edits/{edit}/replan', { params: { path: { slug, edit } } })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.project(slug) })
      qc.invalidateQueries({ queryKey: keys.projects })
    },
  })
}

export function useSettings() {
  return useQuery({ queryKey: keys.settings, queryFn: () => unwrap(api.GET('/api/settings')) })
}

export function useSaveSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SettingsUpdate) => unwrap(api.PUT('/api/settings', { body })),
    onSuccess: (data) => qc.setQueryData(keys.settings, data),
  })
}

export function useAiLog(slug: string) {
  return useQuery({
    queryKey: keys.aiLog(slug),
    queryFn: () => unwrap(api.GET('/api/projects/{slug}/ai-log', { params: { path: { slug }, query: { limit: 500 } } })),
  })
}

export function useAiEntry(slug: string, id: number | null) {
  return useQuery({
    queryKey: keys.aiEntry(slug, id ?? 0),
    queryFn: () =>
      unwrap(api.GET('/api/projects/{slug}/ai-log/{entry_id}', { params: { path: { slug, entry_id: id! } } })),
    enabled: id !== null,
    staleTime: Infinity,
  })
}

export const FINISHED = ['done', 'failed', 'cancelled']

/** A job's state and progress lines, streamed over SSE. Refreshes the project's data when the job's status changes. */
export function useJobStream(initial: Job | null | undefined) {
  const qc = useQueryClient()
  const [job, setJob] = useState<Job | null>(initial ?? null)
  const [lines, setLines] = useState<JobEvent[]>([])
  const id = initial?.id
  const last = useRef<string | null>(null)

  useEffect(() => {
    if (id === undefined) return
    setLines([])
    last.current = null
    const source = new EventSource(`/api/jobs/${id}/events`)
    source.addEventListener('log', (e) => {
      const event = JSON.parse((e as MessageEvent).data) as JobEvent
      setLines((prev) => (prev.length && prev[prev.length - 1].id >= event.id ? prev : [...prev, event]))
    })
    source.addEventListener('job', (e) => {
      const next = JSON.parse((e as MessageEvent).data) as Job
      setJob(next)
      if (last.current !== next.status) {
        if (last.current !== null && next.project) {
          qc.invalidateQueries({ queryKey: keys.project(next.project) })
          qc.invalidateQueries({ queryKey: ['edit', next.project] })
          qc.invalidateQueries({ queryKey: keys.segments(next.project) })
          qc.invalidateQueries({ queryKey: keys.aiLog(next.project) })
          qc.invalidateQueries({ queryKey: keys.projects })
        }
        last.current = next.status
      }
      if (FINISHED.includes(next.status)) source.close()
    })
    return () => source.close()
  }, [id, qc])

  return { job: job && job.id === id ? job : (initial ?? null), lines }
}

export function useJobActions() {
  const qc = useQueryClient()
  const refresh = (job: Job) => {
    qc.invalidateQueries({ queryKey: keys.projects })
    if (job.project) qc.invalidateQueries({ queryKey: keys.project(job.project) })
  }
  const approve = useMutation({
    mutationFn: (id: number) => unwrap(api.POST('/api/jobs/{job_id}/approve', { params: { path: { job_id: id } } })),
    onSuccess: refresh,
  })
  const cancel = useMutation({
    mutationFn: (id: number) => unwrap(api.POST('/api/jobs/{job_id}/cancel', { params: { path: { job_id: id } } })),
    onSuccess: refresh,
  })
  return { approve, cancel }
}
