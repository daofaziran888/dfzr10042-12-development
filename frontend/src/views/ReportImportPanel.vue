<template>
  <section class="report-import" data-testid="report-import">
    <header class="page-head">
      <div>
        <h3>报告模板批量导入</h3>
        <p class="page-desc">
          按指标编号核验区块与源行；只有已核验区块进入报告，缺少可靠编号的指标单独待核。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="load-sample" @click="loadSample">
          载入示例 CSV
        </button>
        <button class="btn primary" type="button" data-testid="submit-import" :disabled="busy || !csvText.trim()" @click="submitImport">
          {{ busy ? '提交中…' : '导入模板 CSV' }}
        </button>
      </div>
    </header>

    <div class="import-grid">
      <label class="import-file">
        <span>选择 CSV 文件</span>
        <input type="file" accept=".csv,text/csv" data-testid="import-file" @change="pickFile" />
      </label>
      <label class="import-file">
        <span>或直接粘贴 CSV</span>
        <textarea v-model="csvText" rows="4" data-testid="import-text" placeholder="模板编号,报告名称,区块,指标编号,指标名称,源行编号,金额" />
      </label>
    </div>

    <p v-if="message" class="import-message" :class="{ 'error-text': !ok }" data-testid="import-message">
      {{ message }}
    </p>

    <article v-if="current" class="import-card" data-testid="import-result">
      <header class="import-card-head">
        <strong>{{ current.template_id }} · {{ current.report_name }}</strong>
        <span class="tag">{{ current.version_label }} · {{ current.status }}</span>
        <span v-if="current.replayed" class="tag">同一文件复用</span>
      </header>
      <p class="page-desc">
        指纹 {{ current.fingerprint.slice(0, 12) }} · 已核验区块：
        {{ current.verified_blocks.join('、') || '无' }} · 待核区块：
        {{ current.pending_blocks.join('、') || '无' }}
      </p>

      <div v-if="current.status === '待确认' && current.pending_blocks.length" class="scope-conflict" data-testid="scope-conflict">
        <strong>部分启用与整份启用冲突：</strong>
        已核验区块可按部分启用生效；整份启用会因
        {{ current.pending_blocks.join('、') }} 仍缺证据而被拒绝。
        <div class="page-actions">
          <button class="btn" type="button" data-testid="confirm-partial" :disabled="busy" @click="confirm('partial')">
            按部分启用
          </button>
          <button class="btn" type="button" data-testid="confirm-whole" :disabled="busy" @click="confirm('whole')">
            按整份启用
          </button>
        </div>
      </div>
      <div v-else-if="current.status === '待确认'" class="page-actions" style="margin-bottom: 10px">
        <button class="btn primary" type="button" data-testid="confirm-whole" :disabled="busy" @click="confirm('whole')">
          整份启用
        </button>
      </div>
      <p v-else class="page-desc" data-testid="import-confirmed">
        本次导入已确认，正式使用 {{ current.version_label }}（{{ scopeLabel(current.activation_scope) }}）；
        再次提交同一文件只确认这一版。
      </p>

      <table class="data-table">
        <thead>
          <tr>
            <th>区块</th><th>状态</th><th>指标编号</th><th>指标名称</th>
            <th>口径</th><th>源行</th><th>金额</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="block in current.blocks" :key="block.block">
            <tr v-for="metric in block.metrics" :key="`${block.block}-${metric.stable_key}`">
              <td>{{ block.block }}</td>
              <td>{{ block.status }}</td>
              <td>{{ metric.metric_code }}</td>
              <td>{{ metric.metric_name }}</td>
              <td>{{ metric.aggregation }} / {{ metric.formula }}</td>
              <td>{{ metric.source_rows.map((row) => row.row_id).join('、') }}</td>
              <td>{{ metric.value }}</td>
            </tr>
            <tr v-if="!block.verified" :key="`${block.block}-pending`" data-testid="pending-block">
              <td>{{ block.block }}</td>
              <td>{{ block.status }}</td>
              <td colspan="5">
                <span v-for="item in block.pending_items" :key="`${item.line}-${item.reason}`" class="pending-item">
                  第 {{ item.line }} 行 · {{ item.metric_code || '无编号' }} ·
                  {{ item.metric_name || '—' }} · {{ item.reason }}
                </span>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </article>

    <article class="import-card" data-testid="computable-templates">
      <header class="import-card-head">
        <strong>可计算模板清单</strong>
        <span class="tag">按指标编号读取查询行</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>模板编号</th><th>版本</th><th>状态</th><th>启用范围</th>
            <th>已核验区块</th><th>待核区块</th><th>可计算指标</th><th>合计金额</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in templates" :key="`${item.template_id}-${item.version_label}`">
            <td>{{ item.template_id }}</td>
            <td>{{ item.version_label }}</td>
            <td>{{ item.status }}</td>
            <td>{{ scopeLabel(item.activation_scope) }}</td>
            <td>{{ item.verified_blocks.join('、') || '—' }}</td>
            <td>{{ item.pending_blocks.join('、') || '—' }}</td>
            <td>{{ item.metrics.map((metric) => `${metric.metric_code}=${metric.value}`).join('；') || '—' }}</td>
            <td>{{ item.total_amount }}</td>
          </tr>
          <tr v-if="!templates.length">
            <td colspan="8" class="empty-state">暂无已导入的模板</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="report" class="import-card" data-testid="report-data">
      <header class="import-card-head">
        <strong>当前报告数据 · {{ report.report_name }}</strong>
        <span class="tag">正式使用 {{ report.template_version_label }}</span>
        <span class="tag">合计 {{ report.total_amount }}</span>
      </header>
      <p class="page-desc">
        待核区块：{{ report.pending_blocks.join('、') || '无' }} ·
        未核验指标不入报：{{ report.excluded_metrics.length }} 项
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>区块</th><th>指标编号</th><th>指标名称</th><th>稳定身份</th>
            <th>源行</th><th>计算范围</th><th>金额</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in report.metrics" :key="metric.stable_key">
            <td>{{ metric.block }}</td>
            <td>{{ metric.metric_code }}</td>
            <td>{{ metric.metric_name }}</td>
            <td>{{ metric.metric_identity }}<br /><small>{{ metric.definition_fingerprint }}</small></td>
            <td>{{ metric.source_rows.map((row) => `${row.row_id}=${row.amount}`).join('、') }}</td>
            <td>{{ metric.calculation_scope.query_scope }}</td>
            <td>{{ metric.value }}</td>
          </tr>
          <tr v-for="item in report.excluded_metrics" :key="`${item.line}-${item.source_row_id}`" data-testid="excluded-metric">
            <td>{{ item.block }}</td>
            <td>{{ item.metric_code || '无编号' }}</td>
            <td>{{ item.metric_name || '—' }}</td>
            <td colspan="4">待核，未计入报告：{{ item.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="Object.keys(report.same_name_codes).length" class="page-desc">
        同名不同编号：
        <span v-for="(others, code) in report.same_name_codes" :key="code">
          {{ code }} 与 {{ others.join('、') }}；
        </span>
      </p>
      <p v-if="report.candidate_metrics.length" class="page-desc" data-testid="candidate-report">
        待确认模板可算得：
        {{ report.candidate_metrics.map((metric) => `${metric.metric_code}=${metric.value}`).join('；') }}
        （合计 {{ report.candidate_total_amount }}），确认后才替换正式版本。
      </p>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  confirmReportImport,
  importReportCsv,
  loadComputableTemplates,
  loadReport,
  sampleReportCsv,
  type ComputableTemplate,
  type ImportSnapshot,
  type ReportData,
} from '@/api/backend-service'

const csvText = ref('')
const fileName = ref('')
const busy = ref(false)
const ok = ref(true)
const message = ref('')
const current = ref<ImportSnapshot | null>(null)
const templates = ref<ComputableTemplate[]>([])
const report = ref<ReportData | null>(null)

function scopeLabel(scope: string | null): string {
  if (scope === 'partial') return '部分启用'
  if (scope === 'whole') return '整份启用'
  if (scope === 'legacy') return '旧模板'
  return '未启用'
}

async function refresh() {
  templates.value = await loadComputableTemplates()
  report.value = await loadReport()
}

async function loadSample() {
  message.value = ''
  try {
    csvText.value = await sampleReportCsv()
  } catch (error) {
    ok.value = false
    message.value = error instanceof Error ? error.message : '示例读取失败'
  }
}

async function pickFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  fileName.value = file.name
  csvText.value = await file.text()
}

async function submitImport() {
  busy.value = true
  message.value = ''
  try {
    const result = await importReportCsv(csvText.value, fileName.value)
    ok.value = result.ok
    message.value = result.message
    if (result.data?.import) current.value = result.data.import
    await refresh()
  } catch (error) {
    ok.value = false
    message.value = error instanceof Error ? error.message : '导入失败'
  } finally {
    busy.value = false
  }
}

async function confirm(scope: 'partial' | 'whole') {
  if (!current.value) return
  busy.value = true
  message.value = ''
  try {
    const result = await confirmReportImport(current.value.import_id, scope)
    ok.value = result.ok
    message.value = result.message
    if (result.data?.import) current.value = result.data.import
    await refresh()
  } catch (error) {
    ok.value = false
    message.value = error instanceof Error ? error.message : '确认失败'
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void refresh().catch(() => {
    templates.value = []
    report.value = null
  })
})
</script>

<style scoped>
.report-import { margin-bottom: 24px; }
.import-grid { display: grid; grid-template-columns: 1fr 2fr; gap: 12px; margin-bottom: 8px; }
.import-file span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.import-file textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: monospace; font-size: 12px; }
.import-message { font-size: 13px; margin: 8px 0; }
.import-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.import-card-head { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.tag { font-size: 12px; background: #eef2ff; color: #3730a3; border-radius: 4px; padding: 2px 6px; }
.scope-conflict { border: 1px solid #f0c36d; background: #fff8e6; border-radius: 6px; padding: 10px; font-size: 13px; margin: 8px 0; }
.pending-item { display: block; color: #b42318; }
</style>
