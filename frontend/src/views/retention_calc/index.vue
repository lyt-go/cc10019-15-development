<template>
  <section class="page" data-module="retention_calc">
    <header class="page-head">
      <div>
        <h2>留存计算</h2>
        <p class="page-desc">维护留存率周期定义与曲线版本；LTV 校准采用曲线时登记采用记录及入组批次，影响基准群的迟到更正未完成前会阻断曲线输入继续生效。</p>
      </div>
    </header>

    <div class="analysis-summary">
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">留存配置</span>
        <strong style="font-size:22px;display:block">{{ configs.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">曲线版本</span>
        <strong style="font-size:22px;display:block">{{ curves.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">校准采用记录</span>
        <strong style="font-size:22px;display:block">{{ adoptions.length }}</strong>
      </article>
      <article class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">更正中</span>
        <strong style="font-size:22px;display:block">{{ openCorrectionCount }}</strong>
      </article>
    </div>

    <p v-if="message" :class="messageError ? 'error-text' : 'page-desc'" style="margin:8px 0">{{ message }}</p>

    <form class="analysis-form" @submit.prevent="runCalc">
      <label><span>留存配置</span>
        <select v-model="selectedConfig" data-testid="run-config">
          <option v-for="c in configs" :key="c.config_id" :value="c.config_id">
            {{ c.config_id }}（{{ c.init_event }} → {{ c.return_event }}）
          </option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">发起计算（固化曲线版本）</button>
    </form>

    <!-- 最近一次曲线的留存率柱状图 -->
    <div v-if="chartRates.length" class="analysis-chart" data-testid="retention-chart">
      <div
        v-for="(rate, i) in chartRates"
        :key="i"
        class="analysis-bar"
        :style="{ height: barHeight(rate) + 'px' }"
      ><span>{{ rate }}</span></div>
    </div>

    <h3 style="font-size:15px;margin:16px 0 8px">留存配置</h3>
    <table class="data-table">
      <thead><tr><th>配置编号</th><th>初始行为</th><th>回访行为</th><th>计算周期</th><th>基准日期</th><th>留存窗口</th><th>当前生效曲线</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="c in configs" :key="c.id" :data-status="c.status">
          <td>{{ c.config_id }}</td><td>{{ c.init_event }}</td><td>{{ c.return_event }}</td>
          <td>{{ c.calc_period }}</td><td>{{ c.base_date }}</td><td>{{ c.retention_window }}</td>
          <td>{{ c.active_curve_version ?? '—' }}</td><td>{{ c.status }}</td>
        </tr>
      </tbody>
    </table>

    <h3 style="font-size:15px;margin:16px 0 8px">留存曲线版本（带指纹，历史版本可回滚复算）</h3>
    <table class="data-table" data-testid="curve-table">
      <thead><tr><th>曲线版本</th><th>留存配置</th><th>指纹</th><th>数据修订</th><th>入组批次</th><th>基准群规模</th><th>生成时间</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="cv in curves" :key="cv.curve_version" :data-testid="`curve-row-${cv.curve_version}`">
          <td>{{ cv.curve_version }}</td>
          <td>{{ cv.retention_config_id }}</td>
          <td><code style="font-size:11px">{{ cv.curve_fingerprint }}</code></td>
          <td>{{ cv.data_revision ?? 0 }}</td>
          <td>{{ cv.cohort_batches.length }} 个</td>
          <td><span v-for="(size, key) in cv.cohort_sizes" :key="key" style="margin-right:6px">{{ key }}:{{ size }}</span></td>
          <td>{{ cv.created_at }}</td>
          <td><span :class="['badge', curveBlocked(cv.curve_fingerprint) ? 'badge-red' : 'badge-green']">
            {{ curveBlocked(cv.curve_fingerprint) ? '更正阻断中' : cv.status }}
          </span></td>
        </tr>
        <tr v-if="!curves.length"><td colspan="8" class="empty-state">暂无曲线版本，请先发起计算</td></tr>
      </tbody>
    </table>

    <h3 style="font-size:15px;margin:16px 0 8px">校准采用记录（已被 LTV 采用的曲线及入组批次）</h3>
    <table class="data-table" data-testid="adoption-table">
      <thead><tr><th>采用记录</th><th>留存配置</th><th>曲线版本</th><th>曲线指纹</th><th>入组批次</th><th>采用模型</th><th>来源明细</th><th>采用时间</th></tr></thead>
      <tbody>
        <tr v-for="a in adoptions" :key="a.adoption_id" :data-testid="`adoption-row-${a.adoption_id}`">
          <td>{{ a.adoption_id }}</td><td>{{ a.retention_config_id }}</td>
          <td>{{ a.curve_version }}</td><td><code style="font-size:11px">{{ a.curve_fingerprint }}</code></td>
          <td>{{ a.cohort_batches.length }} 个</td><td>{{ a.adopted_by_model }}</td>
          <td>{{ a.source_detail_id }}</td><td>{{ a.adopted_at }}</td>
        </tr>
        <tr v-if="!adoptions.length"><td colspan="8" class="empty-state">暂无采用记录</td></tr>
      </tbody>
    </table>

    <h3 style="font-size:15px;margin:16px 0 8px">影响基准群的迟到更正</h3>
    <div class="analysis-form">
      <label><span>更正原因</span><input v-model="correctionReason" placeholder="如：基准群入组事件迟到" data-testid="correction-reason" /></label>
      <button class="btn primary" type="button" data-testid="open-correction" @click="openCorrection">登记迟到更正</button>
    </div>
    <table class="data-table" data-testid="correction-table">
      <thead><tr><th>更正单</th><th>留存配置</th><th>受影响曲线</th><th>受影响批次</th><th>原因</th><th>状态</th><th>替代曲线</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="fx in corrections" :key="fx.correction_id" :data-testid="`correction-row-${fx.correction_id}`" :data-status="fx.status">
          <td>{{ fx.correction_id }}</td><td>{{ fx.retention_config_id }}</td>
          <td>{{ fx.affected_curve_version }}</td>
          <td>{{ fx.affected_batches.length }} 个</td>
          <td>{{ fx.reason }}</td>
          <td><span :class="['badge', fx.status === '更正中' ? 'badge-red' : 'badge-gray']">{{ fx.status }}</span></td>
          <td>{{ fx.replacement_curve_version || '—' }}</td>
          <td class="row-actions">
            <template v-if="fx.status === '更正中'">
              <button class="link" type="button" :data-testid="`complete-${fx.correction_id}`" @click="completeCorrection(fx)">完成更正（重算）</button>
              <button class="link" type="button" :data-testid="`rollback-${fx.correction_id}`" @click="rollbackCorrection(fx)">回滚更正</button>
            </template>
            <span v-else style="color:var(--muted);font-size:12px">
              {{ fx.status === '已回滚' ? `回滚于 ${fx.rolled_back_at}` : `完成于 ${fx.completed_at}` }}
            </span>
          </td>
        </tr>
        <tr v-if="!corrections.length"><td colspan="8" class="empty-state">暂无更正单</td></tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/retention-calc'

const configs = ref<Record<string, any>[]>([])
const curves = ref<Record<string, any>[]>([])
const adoptions = ref<Record<string, any>[]>([])
const corrections = ref<Record<string, any>[]>([])
const selectedConfig = ref('RC-001')
const correctionReason = ref('')
const message = ref('')
const messageError = ref(false)
const chartRates = ref<number[]>([])

const openCorrectionCount = computed(
  () => corrections.value.filter(c => c.status === '更正中').length,
)

function barHeight(v: number) {
  const max = Math.max(...chartRates.value, 0.0001)
  return Math.max(10, (v / max) * 140)
}

function curveBlocked(fingerprint: string) {
  return corrections.value.some(
    c => c.affected_curve_fingerprint === fingerprint && c.status === '更正中',
  )
}

function notify(text: string, error = false) {
  message.value = text
  messageError.value = error
}

async function reload() {
  try {
    const [cfg, cv, ad, fx] = await Promise.all([
      request(`${ENDPOINT}?size=50`).then(x => x.json()),
      request(`${ENDPOINT}/curves`).then(x => x.json()),
      request(`${ENDPOINT}/adoptions`).then(x => x.json()),
      request(`${ENDPOINT}/corrections`).then(x => x.json()),
    ])
    configs.value = cfg.items ?? []
    curves.value = cv.items ?? []
    adoptions.value = ad.items ?? []
    corrections.value = fx.items ?? []
  } catch (e) {
    notify('数据加载失败', true)
  }
}

async function runCalc() {
  try {
    const res = await request(`${ENDPOINT}/${selectedConfig.value}/run`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '计算失败')
    const entry = payload.entry
    const firstKey = Object.keys(entry.retention_rates)[0]
    chartRates.value = (entry.retention_rates[firstKey] ?? []).slice(0, 12)
    notify(`已固化曲线版本 ${entry.curve_version}（指纹 ${entry.curve_fingerprint}）`)
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '留存计算失败', true)
  }
}

async function openCorrection() {
  if (!correctionReason.value.trim()) {
    notify('请填写更正原因', true)
    return
  }
  try {
    const res = await request(`${ENDPOINT}/${selectedConfig.value}/corrections`, {
      method: 'POST',
      body: JSON.stringify({ reason: correctionReason.value }),
    })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '登记更正失败')
    notify(`已登记更正单 ${payload.entry.correction_id}，完成前该曲线输入不可被发布绑定`)
    correctionReason.value = ''
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '登记更正失败', true)
  }
}

async function completeCorrection(fx: Record<string, any>) {
  try {
    const res = await request(`${ENDPOINT}/corrections/${fx.correction_id}/complete`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '完成更正失败')
    notify(`更正完成，替代曲线 ${payload.entry.replacement_curve_version}`)
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '完成更正失败', true)
  }
}

async function rollbackCorrection(fx: Record<string, any>) {
  try {
    const res = await request(`${ENDPOINT}/corrections/${fx.correction_id}/rollback`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.detail || '回滚更正失败')
    notify(`更正已回滚，恢复曲线 ${payload.entry.affected_curve_version}`)
    await reload()
  } catch (e) {
    notify(e instanceof Error ? e.message : '回滚更正失败', true)
  }
}

onMounted(reload)
</script>

<style scoped>
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.badge-green { background: #dcfce7; color: #15803d; }
.badge-red { background: #fee2e2; color: #b91c1c; }
.badge-gray { background: #f1f5f9; color: #64748b; }
code { background: #f1f5f9; padding: 1px 4px; border-radius: 4px; }
</style>
