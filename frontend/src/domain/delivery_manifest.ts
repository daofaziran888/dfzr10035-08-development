/**
 * 投递发布清单前端领域封装。
 *
 * 对应后端 app/services/delivery_manifest.py 与
 * app/routers/delivery_manifest.py，承载分段回执归并、清单状态流转与
 * 分段消费登记的真实业务差异，不走 /api/{module} 通用资源路由。
 *
 * 状态约定（与后端常量保持一致）：
 * - 清单状态：交付中 → 可消费 → 已确认
 * - 分段回执：待回执 / 已签收 / 接收失败 / 超时
 * - 只有「已签收」分段才会出现在可消费清单里；重复签收只认第一次。
 */
import { fetchJson, request } from '@/api/client'

export const MANIFEST_STATUS = {
  delivering: '交付中',
  consumable: '可消费',
  confirmed: '已确认',
} as const

export const RECEIPT_RESULT = {
  acked: '已签收',
  failed: '接收失败',
  timeout: '超时',
} as const

export type SegmentSpec = {
  segment: string
  rows: number
  version?: string | null
}

export type ManifestSegment = {
  分段编号: string
  序号: number
  版本编号: string
  申报行数: number
  实际行数: number | null
  回执状态: string
  交付状态: string
  消费状态: string
  首次签收时间: string | null
  首次消费时间: string | null
  签收次数: number
  重复签收次数: number
}

export type DeliveryManifest = {
  id: number
  清单编号: string
  波次编号: string
  数据主题: string
  订阅方ID: number
  分段总数: number
  已签收分段: number
  失败分段: number
  待回执分段: number
  可消费分段: number
  已消费分段: number
  签收总行数: number
  status: string
  segments: ManifestSegment[]
}

export type ReceiptRecord = {
  回执编号: string
  波次编号: string
  分段编号: string
  版本编号: string
  回执结果: string
  回执行数: number | null
  接收时间: string
  是否首次有效签收: boolean
  是否重复回执: boolean
}

export type ConsumptionRecord = {
  消费编号: string
  波次编号: string
  清单编号: string
  订阅方ID: number
  分段编号: string
  版本编号: string
  实际行数: number
  首次消费时间: string
}

export type ConsumptionResult = {
  ok: boolean
  wave_no: string
  manifest_status: string
  newly_consumed: ConsumptionRecord[]
  already_consumed: ConsumptionRecord[]
  not_available: { 分段编号: string; 版本编号: string; 原因: string }[]
  consumable_segments: {
    分段编号: string
    版本编号: string
    实际行数: number
    消费状态: string
  }[]
}

const BASE = '/api/delivery-manifest'

/** 投递结束建档：建立波次、发布清单，并按分段预登记待消费数据版本。 */
export async function openWave(params: {
  waveNo?: string
  topic: string
  subscriberId: number
  segments: SegmentSpec[]
}): Promise<{ ok: boolean; manifest: DeliveryManifest }> {
  const response = await request(`${BASE}/waves`, {
    method: 'POST',
    body: JSON.stringify({
      wave_no: params.waveNo ?? null,
      topic: params.topic,
      subscriber_id: params.subscriberId,
      segments: params.segments,
    }),
  })
  if (!response.ok) throw new Error('投递波次建档失败')
  return await response.json()
}

/** 采集分段投递回执；重复的成功签收会被后端判为重复回执且不改清单。 */
export async function receiveReceipt(params: {
  waveNo: string
  segment: string
  result: string
  rows?: number | null
  receivedAt?: string | null
}): Promise<{
  ok: boolean
  accepted: boolean
  duplicate: boolean
  message: string
  manifest: DeliveryManifest
}> {
  const response = await request(`${BASE}/receipts`, {
    method: 'POST',
    body: JSON.stringify({
      wave_no: params.waveNo,
      segment: params.segment,
      result: params.result,
      rows: params.rows ?? null,
      received_at: params.receivedAt ?? null,
    }),
  })
  if (!response.ok) throw new Error('回执采集失败')
  return await response.json()
}

/** 订阅管理确认消费：只登记可消费分段，已消费分段不重算。 */
export function confirmConsumption(
  waveNo: string,
  subscriberId: number,
): Promise<ConsumptionResult> {
  return request(`${BASE}/consumption`, {
    method: 'POST',
    body: JSON.stringify({ wave_no: waveNo, subscriber_id: subscriberId }),
  }).then(async (response) => {
    if (!response.ok) throw new Error('消费确认失败')
    return await response.json()
  })
}

export function getManifest(waveNo: string): Promise<DeliveryManifest> {
  return fetchJson<DeliveryManifest>(`${BASE}/waves/${waveNo}/manifest`)
}

export function listReceipts(
  waveNo: string,
): Promise<{ items: ReceiptRecord[]; total: number }> {
  return fetchJson(`${BASE}/waves/${waveNo}/receipts`)
}

export function getSegmentReceipt(
  waveNo: string,
  segment: string,
): Promise<{
  回执状态: string
  实际行数: number | null
  首次签收时间: string | null
  首次有效回执: ReceiptRecord | null
  全部回执: ReceiptRecord[]
}> {
  return fetchJson(`${BASE}/waves/${waveNo}/segments/${segment}/receipt`)
}

export function listConsumption(params: {
  subscriberId?: number
  waveNo?: string
}): Promise<{ items: ConsumptionRecord[]; total: number }> {
  const query = new URLSearchParams()
  if (params.subscriberId !== undefined) {
    query.set('subscriber_id', String(params.subscriberId))
  }
  if (params.waveNo !== undefined) query.set('wave_no', params.waveNo)
  return fetchJson(`${BASE}/consumption?${query.toString()}`)
}
