import { request } from './client'
import { moduleMeta } from '@/data/modules'
import { fetchJson } from './client'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ============================================================
// 报告模板导入 / 可计算清单 / 报告数据
// ============================================================

export type ImportPendingItem = {
  line: number | null
  block: string
  metric_code: string
  metric_name: string
  source_row_id: string
  reason: string
}

export type VerifiedMetric = {
  metric_code: string
  metric_name: string
  metric_identity: string
  stable_key: string
  definition_fingerprint: string
  aggregation: string
  formula: string
  block: string
  value: number
  source_rows: { row_id: string; amount: number }[]
  calculation_scope: { source_row_ids: string[]; row_count: number; query_scope: string }
  template_version?: number
}

export type ImportBlock = {
  block: string
  status: string
  verified: boolean
  metrics: VerifiedMetric[]
  pending_items: ImportPendingItem[]
  pending_reasons: string[]
  preview_value: number
}

export type ImportSnapshot = {
  import_id: string
  fingerprint: string
  template_id: string
  report_name: string
  template_version: number
  version_label: string
  status: string
  activation_scope: string | null
  created_at: string
  confirmed_at: string | null
  confirmations: number
  replayed: boolean
  blocks: ImportBlock[]
  verified_blocks: string[]
  pending_blocks: string[]
  pending_items: ImportPendingItem[]
  verified_metrics: VerifiedMetric[]
  same_name_codes: Record<string, string[]>
  scope_conflict: Record<string, unknown> | null
}

export type ImportResult = ActionResult & {
  data?: { import?: ImportSnapshot; report?: ReportData }
}

export type ComputableTemplate = {
  template_id: string
  report_name: string
  template_version: number
  version_label: string
  status: string
  activation_scope: string | null
  computable: boolean
  fully_verified: boolean
  verified_blocks: string[]
  pending_blocks: string[]
  metrics: VerifiedMetric[]
  total_amount: number
}

export type ReportData = {
  template_id: string
  report_name: string
  template_version: number
  template_version_label: string
  activation_scope: string | null
  status: string
  metrics: VerifiedMetric[]
  total_amount: number
  pending_blocks: string[]
  excluded_metrics: ImportPendingItem[]
  candidate_metrics: VerifiedMetric[]
  candidate_total_amount: number
  same_name_codes: Record<string, string[]>
  traceability: Record<string, string>
}

export async function importReportCsv(
  csvText: string,
  fileName = '',
): Promise<ImportResult> {
  const response = await request('/api/report_builder/imports', {
    method: 'POST',
    body: JSON.stringify({ csv_text: csvText, file_name: fileName }),
  })
  return await response.json()
}

export async function confirmReportImport(
  importId: string,
  scope: 'partial' | 'whole',
): Promise<ImportResult> {
  const response = await request(`/api/report_builder/imports/${importId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ scope }),
  })
  return await response.json()
}

export async function loadComputableTemplates(): Promise<ComputableTemplate[]> {
  const payload = await fetchJson<{ items: ComputableTemplate[] }>(
    '/api/report_builder/computable-templates',
  )
  return payload.items
}

export async function loadReport(templateId = 'REPO-SALES-2026'): Promise<ReportData> {
  return await fetchJson<ReportData>(
    `/api/report_builder/report?template_id=${encodeURIComponent(templateId)}`,
  )
}

export function sampleReportCsv(): Promise<string> {
  return fetch('/api/report_builder/sample-csv').then((response) => {
    if (!response.ok) throw new Error('示例 CSV 读取失败')
    return response.text()
  })
}
