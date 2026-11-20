<template>
  <section class="page" data-module="lifetime_value">
    <header class="page-head">
      <div>
        <h2>生命周期价值</h2>
        <p class="page-desc">维护 LTV 预测模型与按版本发布的预测明细，明细内嵌发布当时的留存曲线与入组批次，支持校准发布、回滚与沿留存记录复算。</p>
      </div>
    </header>

    <div class="analysis-summary">
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">模型数量</span>
        <strong style="font-size:22px;display:block">{{ models.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">预测明细版本</span>
        <strong style="font-size:22px;display:block">{{ details.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">发布单</span>
        <strong style="font-size:22px;display:block">{{ releases.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">当前生效发布</span>
        <strong style="font-size:16px;display:block">{{ activeRelease?.release_id ?? '—' }}</strong>
      </article>
    </div>

    <p v-if="message" :class="messageError ? 'error-text' : 'page-desc'" style="margin:8px 0">{{ message }}</p>

    <!-- 生成按版本的预测明细：关联留存曲线 -->
    <form class="analysis-form" @submit.prevent="buildDetail">
      <label><span>LTV模型</span>
        <select v-model="form.model_id" data-testid="form-model">
          <option v-for="m in models" :key="m.model_id" :value="m.model_id">{{ m.model_id }}（{{ m.segment_id }}）</option>
        </select>
      </label>
      <label><span>关联留存配置</span>
        <select v-model="form.retention_config_id" data-testid="form-retention">
          <option v-for="r in retentionConfigs" :key="r.config_id" :value="r.config_id">
            {{ r.config_id }} · 生效曲线 {{ r.active_curve_version ?? '未计算' }}
          </option>
        </select>
      </label>
      <label><span>预测周期</span><input v-model="form.cycle" placeholder="如 2026-11" data-testid="form-cycle" /></label>
      <button class="btn primary" type="submit" data-testid="run-analysis">生成预测明细</button>
      <button class="btn" type="button" data-testid="refresh" @click="reload">刷新</button>
    </form>

    <!-- 各版本调整后 LTV 均值对比 -->
    <div v-if="details.length" class="analysis-chart" data-testid="ltv-chart">
      <div
        v-for="d in details"
        :key="d.detail_id"
        class="analysis-bar"
        :style="{ height: barHeight(d.adjusted_avg_ltv) + 'px' }"
      ><span>{{ d.detail_id.slice(-3) }}</span></div>
    </div>

    <h3 style="font-size:15px;margin:16px 0 8px">预测明细（按版本，内嵌当时曲线与逐用户估值）</h3>
    <table class="data-table" data-testid="detail-table">
      <thead>
        <tr>
          <th>明细编号</th><th>模型</th><th>周期</th><th>状态</th>
          <th>关联曲线版本</th><th>曲线指纹</th><th>采用记录</th>
          <th>入组批次</th><th>调整后均值</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in details" :key="d.detail_id" :data-testid="`detail-row-${d.detail_id}`" :data-status="d.status">
          <td>{{ d.detail_id }}</td>
          <td>{{ d.model_id }}</td>
          <td>{{ d.cycle }}</td>
          <td><span :class="['badge', badgeClass(d.status)]">{{ d.status }}</span></td>
          <td>{{ d.curve_version }}</td>
          <td><code style="font-size:11px">{{ d.curve_fingerprint }}</code></td>
          <td>{{ d.adoption_id }}</td>
          <td>{{ d.cohort_batches.length }} 个</td>
          <td>{{ d.adjusted_avg_ltv }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="viewDetail(d)">查看估值</button>
            <button
              v-if="d.status === '待发布'"
              class="link"
              type="button"
              :data-testid="`publish-${d.detail_id}`"
              @click="publish(d)"
            >发布</button>
            <button class="link" type="button" :data-testid="`recompute-${d.detail_id}`" @click="recompute(d)">复算核对</button>
          </td>
        </tr>
        <tr v-if="!details.length"><td colspan="10" class="empty-state">暂无预测明细，请先生成</td></tr>
      </tbody>
    </table>

    <h3 style="font-size:15px;margin:16px 0 8px">校准发布单（生效时间、关联曲线、替换与回滚可核对）</h3>
    <table class="data-table" data-testid="release-table">
      <thead>
        <tr>
          <th>发布单</th><th>模型</th><th>周期</th><th>状态</th><th>明细</th>
          <th>关联曲线</th><th>生效起</th><th>生效止</th><th>替换自</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in releases" :key="r.release_id" :data-testid="`release-row-${r.release_id}`" :data-status="r.status">
          <td>{{ r.release_id }}</td>
          <td>{{ r.model_id }}</td>
          <td>{{ r.cycle }}</td>
          <td><span :class="['badge', badgeClass(r.status)]">{{ r.status }}</span></td>
          <td>{{ r.detail_id }}</td>
          <td>{{ r.curve_version }}</td>
          <td>{{ r.effective_from }}</td>
          <td>{{ r.effective_to || '—' }}</td>
          <td>{{ r.replaced_release_id || '—' }}</td>
          <td class="row-actions">
            <button
              v-if="r.status === '已发布'"
              class="link"
              type="button"
              :data-testid="`rollback-${r.release_id}`"
              @click="rollback(r)"
            >回滚</button>
            <span v-else style="color:var(--muted);font-size:12px">{{ r.rollback_reason || '—' }}</span>
          </td>
        </tr>
        <tr v-if="!releases.length"><td colspan="10" class="empty-state">暂无发布单</td></tr>
      </tbody>
    </table>

    <!-- 明细详情：逐用户估值 + 留存快照 -->
    <div v-if="selectedDetail" class="modal-backdrop" data-testid="detail-modal" @click.self="selectedDetail = null">
      <div class="modal" style="width:min(760px,100%)">
        <h3>预测明细 {{ selectedDetail.detail_id }}</h3>
        <p class="page-desc">
          曲线 {{ selectedDetail.curve_version }}（{{ selectedDetail.curve_fingerprint }}）·
          采用记录 {{ selectedDetail.adoption_id }} · 留存均值 {{ selectedDetail.avg_retention_rate }}
        </p>
        <p class="page-desc">入组批次：
          <span v-for="b in selectedDetail.cohort_batches" :key="b.batch_id" style="margin-right:8px">
            {{ b.segment_key }}({{ b.cohort_size }}人)
          </span>
        </p>
        <table class="data-table" style="max-height:320px;display:block;overflow:auto">
          <thead><tr><th>用户</th><th>基础估值</th><th>预测估值</th><th>留存调整后估值</th></tr></thead>
          <tbody>
            <tr v-for="u in selectedDetail.predicted_values.slice(0, 20)" :key="u.user_id">
              <td>{{ u.user_id }}</td><td>{{ u.base_value }}</td>
              <td>{{ u.predicted_value }}</td><td>{{ u.retention_adjusted_value }}</td>
            </tr>
          </tbody>
        </table>
        <p class="page-desc" style="margin-top:8px">仅展示前 20 个用户，共 {{ selectedDetail.predicted_values.length }} 条估值。</p>
        <div class="page-actions" style="margin-top:12px">
          <button class="btn primary" type="button" @click="selectedDetail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/lifetime-value'
const RETENTION_ENDPOINT = '/api/retention-calc'

interface Detail {
  detail_id: string
  model_id: string
  cycle: string
  status: string
  curve_version: string
  curve_fingerprint: string
  adoption_id: string
  cohort_batches: Array<{ batch_id: string; segment_key: string; cohort_size: number }>
  adjusted_avg_ltv: number
  avg_retention_rate: number
  predicted_values: Array<Record<string, number | string>>
}
interface Release {
  release_id: string
  model_id: string
  cycle: string
  status: string
  detail_id: string
  curve_version: string
  effective_from: string
  effective_to: string
  replaced_release_id: string
  rollback_reason: string
}

const models = ref<Record<string, any>[]>([])
const retentionConfigs = ref<Record<string, any>[]>([])
const details = ref<Detail[]>([])
const releases = ref<Release[]>([])
const activeRelease = ref<Release | null>(null)
const selectedDetail = ref<Detail | null>(null)
const form = ref({ model_id: 'LTV-001', retention_config_id: 'RC-001', cycle: '2026-11' })
const message = ref('')
const messageError = ref(false)

function badgeClass(status: string) {
  if (status === '已发布') return 'badge-green'
  if (status === '待发布') return 'badge-blue'
  return 'badge-gray'
}

function barHeight(v: number) {
  const max = Math.max(...details.value.map(d => d.adjusted_avg_ltv), 0.0001)
  return Math.max(10, (v / max) * 140)
}

function notify(text: string, error = false) {
  message.value = text
  messageError.value = error
}

async function reload() {
  try {
    const [m, rc, d, r] = await Promise.all([
      request(`${ENDPOINT}?size=50`).then(x => x.json()),
      request(`${RETENTION_ENDPOINT}?size=50`).then(x => x.json()),
      request(`${ENDPOINT}/forecast-details`).then(x => x.json()),
      request(`${ENDPOINT}/releases`).then(x => x.json()),
    ])
    models.value = m.items ?? []
    retentionConfigs.value = rc.items ?? []
    details.value = (d.items ?? []) as Detail[]
    releases.value = (r.items ?? []) as Release[]
    activeRelease.value = releases.value.find(x => x.status === '已发布') ?? null
  } catch (e) {
    notify('数据加载失败', true)
  }
}

async function buildDetail() {
  try {
    const res = await request(`${ENDPOINT}/${form.value.model_id}/forecast-details`, {
      method: 'POST',
      body: JSON.stringify({
        retention_config_id: form.value.retention_config_id,
        cycle: form.value.cycle,
      }),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || payload.message || '生成失败')
    notify(`已生成预测明细 ${payload.entry.detail_id}，关联曲线 ${payload.entry.curve_version}`)
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '生成预测明细失败', true)
  }
}

async function publish(detail: Detail) {
  try {
    const res = await request(`${ENDPOINT}/forecast-details/${detail.detail_id}/publish`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '发布失败')
    notify(`发布成功：${payload.entry.release_id}，生效时间 ${payload.entry.effective_from}`)
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '发布失败', true)
  }
}

async function rollback(release: Release) {
  try {
    const res = await request(`${ENDPOINT}/releases/${release.release_id}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ reason: '页面手动回滚' }),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '回滚失败')
    const restored = payload.entry.restored_release
    notify(restored ? `已回滚，恢复发布单 ${restored.release_id}` : '已回滚，无历史发布可恢复')
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '回滚失败', true)
  }
}

async function recompute(detail: Detail) {
  try {
    const res = await request(`${ENDPOINT}/forecast-details/${detail.detail_id}/recompute`)
    if (!res.ok) throw new Error('复算失败')
    const data = await res.json()
    notify(
      `复算 ${detail.detail_id}：指纹${data.fingerprint_matches ? '一致 ✓' : '不一致 ✗'}，` +
      `复算均值 ${data.recomputed_adjusted_avg_ltv}（登记 ${data.stored_adjusted_avg_ltv}）`,
      !data.fingerprint_matches,
    )
  } catch (e) {
    notify(e instanceof Error ? e.message : '复算失败', true)
  }
}

function viewDetail(detail: Detail) {
  selectedDetail.value = detail
}

onMounted(reload)
</script>

<style scoped>
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge-green { background: #dcfce7; color: #15803d; }
.badge-blue { background: #dbeafe; color: #1d4ed8; }
.badge-gray { background: #f1f5f9; color: #64748b; }
code { background: #f1f5f9; padding: 1px 4px; border-radius: 4px; }
</style>
