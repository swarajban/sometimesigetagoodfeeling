import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

export const api = createClient<paths>({ baseUrl: '', headers: { 'X-Goodfeeling': '1' } })

type S = components['schemas']
export type Job = S['Job']
export type JobEvent = S['JobEvent']
export type Estimate = S['Estimate']
export type ProjectSummary = S['ProjectSummary']
export type ProjectDetail = S['ProjectDetail']
export type EditSummary = S['EditSummary']
export type EditDetail = S['EditDetail']
export type PlanShot = S['PlanShot']
export type Segment = S['SegmentOut']
export type Moment = S['MomentOut']
export type Choice = S['Choice']
export type ChoiceUpdate = S['ChoiceUpdate']
export type MusicInfo = S['MusicInfo']
export type SettingsOut = S['SettingsOut']
export type SettingsUpdate = S['SettingsUpdate']
export type ImportRequest = S['ImportRequest']
export type ImportCheck = S['ImportCheck']
export type AiLogPage = S['AiLogPage']
export type AiLogDetail = S['AiLogDetail']
export type FsListing = S['FsListing']

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

function detail(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('detail' in error)) return undefined
  const d = (error as { detail: unknown }).detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map((e) => (e && typeof e === 'object' && 'msg' in e ? String(e.msg) : String(e))).join('; ')
  return undefined
}

export async function unwrap<T>(call: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await call
  if (error !== undefined || data === undefined) {
    throw new ApiError(detail(error) ?? response.statusText ?? 'Request failed', response.status)
  }
  return data
}
