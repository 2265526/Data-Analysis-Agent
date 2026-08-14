<template>
  <div>
    <div class="page-header">
      <h2>📈 指标看板</h2>
      <p>平台运行统计与成本核算(数据来自 Prometheus 埋点 + 数据库聚合)</p>
    </div>

    <!-- 统计卡 -->
    <el-row :gutter="16" style="margin-bottom: 18px">
      <el-col v-for="s in statCards" :key="s.lbl" :span="5">
        <div class="stat-card">
          <div class="num">{{ s.num }}</div>
          <div class="lbl">{{ s.lbl }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 图表区 -->
    <el-row :gutter="16">
      <el-col :span="12">
        <div class="panel-card">
          <div ref="pieEl" class="chart" />
        </div>
      </el-col>
      <el-col :span="12">
        <div class="panel-card">
          <div ref="trendEl" class="chart" />
        </div>
      </el-col>
      <el-col :span="24" style="margin-top: 16px">
        <div class="panel-card">
          <div ref="nodeEl" class="chart chart-lg" />
        </div>
      </el-col>
      <el-col :span="12" style="margin-top: 16px">
        <div class="panel-card">
          <div id="tokenTypeChart" class="chart" />
        </div>
      </el-col>
      <el-col :span="12" style="margin-top: 16px">
        <div class="panel-card">
          <div id="nodeRunChart" class="chart" />
        </div>
      </el-col>
    </el-row>

    <!-- 进程内指标 -->
    <div v-if="hasProm" class="panel-card" style="margin-top: 18px">
      <h3 style="margin-top: 0">⚙️ 进程内指标 <span class="proc-note">本进程启动后累计, 重启服务清零; 执行分析任务后自动生成</span></h3>
      <el-empty
        v-if="!hasPromData"
        description="本进程启动后暂无指标数据: 重启服务会清零进程内指标, 执行任一分析任务后自动恢复"
        :image-size="60"
        style="padding: 10px 0"
      />
      <el-row v-else :gutter="16">
        <el-col
          v-for="(m, name) in promAll"
          :key="name"
          :xs="24" :sm="12" :lg="8"
          style="margin-bottom: 16px"
        >
          <div class="metric-card">
            <div class="metric-title">{{ METRIC_META[name]?.cn || name }}</div>
            <div class="metric-chart" :id="`prom-chart-${name}`" />
          </div>
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import * as echarts from 'echarts'
import { fetchMetrics } from '@/api'

const BRAND = '#4f46e5'
const PALETTE = ['#4f46e5', '#0ea5e9', '#f59e0b', '#ef4444', '#10b981', '#8b5cf6']

// 进程内指标 → 中文名(展示层映射, 指标名本身保持 Prometheus 规范)
const METRIC_META = {
  task_executed_total: { cn: '任务执行总数' },
  task_retry_count: { cn: '任务重试次数分布' },
  self_heal_successes_total: { cn: '自修复成功数' },
  self_heal_failures_total: { cn: '自修复失败数' },
  cache_hits_total: { cn: '缓存命中数' },
  cache_misses_total: { cn: '缓存未命中数' },
  rate_limit_rejections_total: { cn: '限流拒绝数' },
  tool_param_rejections_total: { cn: '工具参数拒绝数' },
  executor_failures_total: { cn: '沙箱执行失败数' },
  sandbox_exec_duration_seconds: { cn: '沙箱执行耗时分布' },
  llm_tokens_total: { cn: 'LLM Token 消耗' },
  circuit_breaker_trips_total: { cn: '熔断触发次数' }
}

// 标签键 → 中文
const LABEL_CN = {
  node: '节点', type: '类型', status: '状态', tool: '工具', reason: '原因',
  backend: '沙箱后端', key_prefix: '缓存前缀', key: '限流键'
}

const pieEl = ref()
const trendEl = ref()
const nodeEl = ref()
let pieChart, trendChart, nodeChart, tokenTypeChart, nodeRunChart

const db = reactive({ task_total: 0, user_count: 0, cost_actual: 0, retry_count: 0, tokens: { prompt: 0, completion: 0 }, task_status: {}, trend_7d: [], node_stats: [] })
const prom = ref({})
// 补零: 保证 METRIC_META 中的全部指标始终展示(未触发的显示 0/暂无数据)
const promAll = computed(() => {
  const out = {}
  for (const name of Object.keys(METRIC_META)) {
    out[name] = prom.value[name] || { type: 'counter', series: [] }
  }
  return out
})
const hasProm = computed(() => Object.keys(METRIC_META).length > 0)
// 是否存在至少一条指标数据(任一指标有 series); 全空时显示空态提示而非 12 张空图
const hasPromData = computed(() =>
  Object.values(promAll.value).some((m) => (m.series || []).length > 0)
)

const statCards = computed(() => [
  { num: db.task_total, lbl: '任务总数' },
  { num: `${db.completion_rate ?? 0}%`, lbl: '任务完成率' },
  { num: `${db.self_heal_rate ?? 0}%`, lbl: '自修复率(重试占比)' },
  { num: `¥${Number(db.cost_actual).toFixed(4)}`, lbl: '实际成本' },
  { num: (db.tokens.prompt + db.tokens.completion).toLocaleString(), lbl: 'Token 总量' },
  { num: db.audit_log_count ?? 0, lbl: '审计日志' }
])

function labelText(labels) {
  const entries = Object.entries(labels || {})
  return entries.length
    ? entries.map(([k, v]) => `${LABEL_CN[k] || k}=${v}`).join(', ')
    : '全局(无标签)'
}

function renderCharts() {
  // 任务状态分布
  const status = db.task_status || {}
  pieChart = echarts.init(pieEl.value)
  pieChart.setOption({
    title: { text: '任务状态分布', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '52%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        data: Object.entries(status).map(([k, v]) => ({ name: k, value: v }))
      }
    ],
    color: PALETTE
  })

  // 近 7 天趋势
  const trend = db.trend_7d || []
  trendChart = echarts.init(trendEl.value)
  trendChart.setOption({
    title: { text: '近 7 天任务数', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 44, bottom: 28 },
    xAxis: { type: 'category', data: trend.map((t) => t.date) },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'bar', data: trend.map((t) => t.count), itemStyle: { color: BRAND, borderRadius: [6, 6, 0, 0] }, barWidth: 28 }]
  })

  // 节点 Token + 执行次数
  const nodes = db.node_stats || []
  nodeChart = echarts.init(nodeEl.value)
  nodeChart.setOption({
    title: { text: '各节点 Token 消耗与执行次数', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, right: 8 },
    grid: { left: 56, right: 56, top: 44, bottom: 28 },
    xAxis: { type: 'category', data: nodes.map((n) => n.node) },
    yAxis: [
      { type: 'value', name: 'Token' },
      { type: 'value', name: '次数', splitLine: { show: false } }
    ],
    series: [
      { name: 'Token', type: 'bar', data: nodes.map((n) => n.tokens), itemStyle: { color: BRAND, borderRadius: [6, 6, 0, 0] } },
      { name: '执行次数', type: 'line', yAxisIndex: 1, data: nodes.map((n) => n.runs), smooth: true, itemStyle: { color: '#0ea5e9' } }
    ]
  })

  // Token 分类型(prompt/completion)饼图
  const tEl = document.getElementById('tokenTypeChart')
  if (tEl && (db.tokens.prompt > 0 || db.tokens.completion > 0)) {
    tokenTypeChart = echarts.init(tEl)
    tokenTypeChart.setOption({
      title: { text: 'Token 类型占比', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
      tooltip: { trigger: 'item' },
      series: [{
        type: 'pie', radius: ['40%', '66%'], center: ['50%', '55%'],
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { formatter: '{b}: {c}' },
        data: [
          { name: 'Prompt', value: db.tokens.prompt },
          { name: 'Completion', value: db.tokens.completion }
        ]
      }],
      color: ['#4f46e5', '#0ea5e9']
    })
  }

  // 节点执行: 成功 vs 重试对比
  const nEl = document.getElementById('nodeRunChart')
  if (nEl) {
    nodeRunChart = echarts.init(nEl)
    const total = db.node_total_runs || 0
    const retries = db.node_retries || 0
    nodeRunChart.setOption({
      title: { text: '节点执行统计', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
      tooltip: { trigger: 'item' },
      series: [{
        type: 'pie', radius: ['40%', '66%'], center: ['50%', '55%'],
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
        label: { formatter: '{b}: {c}' },
        data: [
          { name: '首次执行', value: Math.max(total - retries, 0) },
          { name: '重试(自修复)', value: retries }
        ]
      }],
      color: ['#10b981', '#f59e0b']
    })
  }
}

function resizeAll() {
  pieChart?.resize()
  trendChart?.resize()
  nodeChart?.resize()
  tokenTypeChart?.resize()
  nodeRunChart?.resize()
  Object.values(promCharts).forEach((c) => c?.resize())
}

// ---------- 进程内指标图表 ----------
const promCharts = {}

function metricLabel(labels) {
  const entries = Object.entries(labels || {})
  return entries.length ? entries.map(([k, v]) => `${LABEL_CN[k] || k} ${v}`).join(' · ') : '全局'
}

// 单值计数器: 仪表盘
function gaugeOption(value) {
  return {
    tooltip: { formatter: () => `当前值 ${Number(value).toLocaleString()}` },
    series: [{
      type: 'gauge',
      startAngle: 210, endAngle: -30,
      min: 0, max: Math.max(value * 1.2, 1),
      progress: { show: true, width: 14, itemStyle: { color: BRAND } },
      axisLine: { lineStyle: { width: 14 } },
      axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { show: false }, pointer: { show: false },
      detail: { valueAnimation: true, fontSize: 22, offsetCenter: [0, '18%'], formatter: (v) => Number(v).toLocaleString() },
      data: [{ value }]
    }]
  }
}

// 多分类计数器(≤8): 饼图
function pieOption(series) {
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie',
      radius: ['35%', '62%'],
      center: ['50%', '46%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 5, borderColor: '#fff', borderWidth: 2 },
      label: { formatter: '{b}\n{c}', fontSize: 11 },
      data: series.map((s) => ({ name: metricLabel(s.labels), value: s.value }))
    }],
    color: PALETTE
  }
}

// 多分类计数器(>8): 横向柱状图
function hbarOption(series) {
  const data = series.map((s) => ({ name: metricLabel(s.labels), value: s.value }))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 90, right: 20, top: 8, bottom: 24 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: data.map((d) => d.name), axisLabel: { fontSize: 11 } },
    series: [{ type: 'bar', data, barWidth: 14, itemStyle: { color: BRAND, borderRadius: [0, 6, 6, 0] }, label: { show: true, position: 'right', fontSize: 11 } }]
  }
}

// 直方图: 区间柱状图(累积计数 → 每段实际落入数量)
function histogramOption(m, name) {
  const buckets = m.buckets || []
  const isRetry = name === 'task_retry_count'
  const unit = isRetry ? '次' : 's'
  // 横轴标签: 区间语义
  const cats = buckets.map((b, i) => {
    if (i === 0) return isRetry ? `0${unit}` : `≤${b}${unit}`
    const lo = buckets[i - 1]
    if (isRetry) return hi(b, lo)
    return `${lo}~${b}${unit}`
  })
  cats.push(isRetry ? `${buckets[buckets.length - 1] + 1}${unit}+` : `>${buckets[buckets.length - 1]}${unit}`)
  const series = (m.series || []).map((s, i) => {
    const b = s.buckets || []
    // 累积 → 区间增量, 并补最后一桶溢出
    const interval = b.map((v, j) => (j === 0 ? v : Math.max(v - b[j - 1], 0)))
    interval.push(Math.max((s.count || 0) - (b.length ? b[b.length - 1] : 0), 0))
    return {
      name: metricLabel(s.labels),
      type: 'bar',
      data: interval,
      itemStyle: { color: PALETTE[i % PALETTE.length], borderRadius: [4, 4, 0, 0] },
      label: { show: true, position: 'top', fontSize: 10, color: '#6b7280' }
    }
  })
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: series.length > 1 ? { top: 0, right: 8, textStyle: { fontSize: 11 } } : undefined,
    grid: { left: 44, right: 16, top: series.length > 1 ? 36 : 24, bottom: 40 },
    xAxis: {
      type: 'category',
      data: cats,
      axisLabel: { fontSize: 10, rotate: cats.length > 6 ? 40 : 0, interval: 0 }
    },
    yAxis: { type: 'value', minInterval: 1 },
    series
  }
}
function hi(b, lo) {
  return b - lo === 1 ? `${b}次` : `${lo + 1}~${b}次`
}

// LLM Token: 分组柱状图(节点 × Prompt/Completion)
function llmOption(series) {
  const byNode = {}
  series.forEach((s) => {
    const node = s.labels?.node || '未知'
    const type = s.labels?.type || 'prompt'
    byNode[node] = byNode[node] || { prompt: 0, completion: 0 }
    byNode[node][type] = (byNode[node][type] || 0) + s.value
  })
  const nodes = Object.keys(byNode)
  const fmt = (v) => Number(v).toLocaleString()
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const head = params[0] ? `<b>${params[0].axisValue}</b>` : ''
        return head + params.map((p) => `<br/>${p.marker}${p.seriesName}: ${fmt(p.value)}`).join('')
      }
    },
    legend: { top: 0, right: 8, textStyle: { fontSize: 11 } },
    // 节点较多时给旋转后的横轴标签留出纵向空间(与 histogramOption 写法一致)
    grid: { left: 64, right: 20, top: 36, bottom: nodes.length > 6 ? 56 : 28 },
    xAxis: {
      type: 'category',
      data: nodes,
      axisLabel: {
        fontSize: 12,
        interval: 0, // 关闭自动跳号: 默认 'auto' 会按标签宽度/带宽自动隐藏部分节点名(见 ECharts FAQ)
        rotate: nodes.length > 6 ? 40 : 0 // 节点多时旋转 40°, 保证全部横坐标可读
      }
    },
    yAxis: { type: 'value', name: 'Token', axisLabel: { fontSize: 10, formatter: (v) => fmt(v) } },
    series: [
      { name: 'Prompt', type: 'bar', data: nodes.map((n) => byNode[n].prompt), itemStyle: { color: BRAND, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 34, label: { show: true, position: 'top', fontSize: 10, formatter: (p) => fmt(p.value) } },
      { name: 'Completion', type: 'bar', data: nodes.map((n) => byNode[n].completion), itemStyle: { color: '#0ea5e9', borderRadius: [4, 4, 0, 0] }, barMaxWidth: 34, label: { show: true, position: 'top', fontSize: 10, formatter: (p) => fmt(p.value) } }
    ]
  }
}

function renderPromCharts() {
  Object.entries(promAll.value).forEach(([name, m]) => {
    const el = document.getElementById(`prom-chart-${name}`)
    if (!el) return
    if (promCharts[name]) promCharts[name].dispose()
    const chart = echarts.init(el)
    promCharts[name] = chart
    const series = m.series || []
    if (!series.length) {
      // 未触发指标: 显示占位
      chart.setOption({
        title: {
          text: '暂无数据',
          left: 'center', top: 'middle',
          textStyle: { fontSize: 13, color: '#9ca3af', fontWeight: 400 }
        }
      })
    } else if (name === 'llm_tokens_total') chart.setOption(llmOption(series))
    else if (m.type === 'histogram') chart.setOption(histogramOption(m, name))
    else if (series.length <= 1) chart.setOption(gaugeOption(series[0]?.value || 0))
    else if (series.length <= 8) chart.setOption(pieOption(series))
    else chart.setOption(hbarOption(series))
  })
}

async function load() {
  try {
    const data = await fetchMetrics()
    Object.assign(db, data.db)
    prom.value = data.prometheus || {}
    renderCharts()
    await nextTick() // 等 v-if 分支渲染出指标卡容器再初始化图表
    renderPromCharts()
    window.addEventListener('resize', resizeAll)
  } catch (e) {
    /* 已提示 */
  }
}

onMounted(load)
onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAll)
  pieChart?.dispose()
  trendChart?.dispose()
  nodeChart?.dispose()
  tokenTypeChart?.dispose()
  nodeRunChart?.dispose()
  Object.values(promCharts).forEach((c) => c?.dispose())
})
</script>

<style scoped>
.chart { height: 300px; }
.chart-lg { height: 320px; }
.proc-note { font-size: 12px; color: #9ca3af; font-weight: 400; margin-left: 6px; }
.metric-card {
  border: 1px solid #eceff5;
  border-radius: 10px;
  background: #fafbfc;
  padding: 12px;
  height: 100%;
}
.metric-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 6px; }
.metric-chart { height: 190px; }
</style>
