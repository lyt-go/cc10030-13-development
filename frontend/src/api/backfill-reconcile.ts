import { request } from './client'

/** 历史回补·行政区树增量对账批量入口的接口封装。 */

export type SupplementPoint = {
  lat: number
  lon: number
  obs_key?: string
  region_code?: string
  collected_at?: string
}

export type RegionReconBucket = {
  region_code: string
  region_name: string
  pending_count: number
  confirmed_count: number
  suspended_count: number
  signed_count: number
  merged_count: number
  next_batch_count: number
}

export type CarryoverOrder = {
  carryover_id: string
  source_app_no: string
  region_code: string
  region_name: string | null
  obs_keys: string[]
  status: string
  reason: string
}

export type ReconSummary = {
  parent_region: string
  total_records: number
  per_region: RegionReconBucket[]
  open_carryovers: CarryoverOrder[]
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '接口处理失败')
  return payload.data as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '接口处理失败')
  return payload.data as T
}

export function submitSupplementBatch(input: {
  batch_no: string
  parent_region: string
  start_date: string
  end_date: string
  points: SupplementPoint[]
}) {
  return postJson<{
    submitted_points: number
    accepted_points: number
    duplicate_points: number
    suspended_points: number
    per_region_pending: { region_code: string; region_name: string; pending_count: number }[]
    duplicates: { obs_key: string; already_region: string | null; confirmation_id: string | null }[]
    suspended: { record_id: string; obs_key: string; reason: string }[]
  }>('/api/backfill/supplement-batches', input)
}

export function listSupplementRecords(params: {
  parent_region?: string
  region_code?: string
  status?: string
}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => Boolean(value)) as [string, string][],
  )
  return getJson<{ items: Record<string, unknown>[]; total: number }>(
    `/api/backfill/supplement-records?${query.toString()}`,
  )
}

export function lookupObservation(obsKey: string) {
  return getJson<{
    obs_key: string
    status: string
    region_code: string | null
    batch_no: string
    confirmation: { confirmation_id: string; region_code: string } | null
    application: { application_id: string; app_no: string; issued_total: number } | null
    carryover: { carryover_id: string; region_code: string } | null
  }>(`/api/backfill/observations/${encodeURIComponent(obsKey)}`)
}

export function confirmRegion(parentRegion: string, regionCode: string, recordIds?: string[]) {
  return postJson<{
    confirmation_id: string | null
    confirmed_count: number
    delta: number
    region_changed: { record_id: string; new_region: string }[]
    suspended: { record_id: string; reason: string }[]
  }>(`/api/backfill/regions/${encodeURIComponent(regionCode)}/confirmations`, {
    parent_region: parentRegion,
    region_code: regionCode,
    record_ids: recordIds ?? null,
  })
}

export function createApplication(input: {
  app_no: string
  parent_region: string
  baseline_total: number
}) {
  return postJson<{ application_id: string; app_no: string; scope_record_ids: string[] }>(
    '/api/backfill/applications',
    input,
  )
}

export function issueApplication(
  applicationId: string,
  mode: 'confirmed_only' | 'merge_pending',
  mergeTarget?: string,
) {
  return postJson<{
    idempotent: boolean
    application: {
      application_id: string
      baseline_total: number
      delta_total: number
      issued_total: number
      carryover_ids: string[]
      delta_by_region: {
        region_code: string
        confirmed_delta: number
        merged_delta: number
      }[]
    }
    carryover_orders: { carryover_id: string; region_code: string; obs_keys: string[] }[]
  }>(`/api/backfill/applications/${encodeURIComponent(applicationId)}/issue`, {
    mode,
    merge_target: mergeTarget ?? null,
  })
}

export function parentReconcileSummary(parentRegion: string) {
  return getJson<ReconSummary>(
    `/api/backfill/regions/${encodeURIComponent(parentRegion)}/summary`,
  )
}

export function settleCarryover(carryoverId: string) {
  return postJson<{
    carryover_id: string
    region_code: string
    confirmation_id: string | null
    confirmed_count: number
    region_changed: { record_id: string; new_region: string }[]
    suspended: { record_id: string; reason: string }[]
  }>(`/api/backfill/carryovers/${encodeURIComponent(carryoverId)}/settle`, {})
}
