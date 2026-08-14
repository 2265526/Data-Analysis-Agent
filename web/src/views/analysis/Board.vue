<template>
  <div class="board-page" v-loading="loading">
    <div class="page-header">
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <el-button size="small" @click="goBack">← 返回</el-button>
        <h2 style="margin: 0">📊 交互式看板</h2>
        <el-button size="small" type="primary" plain :icon="DataAnalysis" @click="openLineage">🔍 数据溯源</el-button>
      </div>
      <p>{{ board?.title || '加载中…' }}</p>
      <p v-if="board?.intent_text" class="sub">{{ board.intent_text }} · 生成于 {{ fmtTime(board.generated_at) }}</p>
    </div>

    <!-- 下钻筛选状态 -->
    <div v-if="activeCategory" class="filter-bar">
      <el-tag closable type="primary" size="large" @close="clearFilter">
        🔍 已下钻: {{ activeCategory }} —— 点击图表其他品类可切换, 明细表已过滤
      </el-tag>
    </div>

    <!-- 数据溯源抽屉: 报告数字来源可核验 -->
    <el-drawer v-model="lineageVisible" title="数据溯源 · 报告数字核验" size="520px">
      <div v-if="lineageLoading" v-loading="true" style="height: 120px"></div>
      <template v-else>
        <el-empty v-if="!runs.length" description="该任务无 SQL 执行记录(可能是简洁问答)" />
        <div v-for="r in runs" :key="r.id" class="lineage-item">
          <div class="lineage-head">
            <el-tag size="small" type="info">步骤 {{ r.run_order }}</el-tag>
            <el-tag size="small">{{ r.rows_returned }} 行</el-tag>
            <el-tag size="small" type="warning">{{ r.duration_ms }}ms</el-tag>
            <el-button size="small" text type="primary" :loading="r._loading" @click="rerun(r)">重跑看明细</el-button>
          </div>
          <div class="lineage-sql">
            <pre>{{ r.sql_text }}</pre>
            <div v-if="r.tables && r.tables.length" class="lineage-tables">
              涉及表: <el-tag v-for="t in r.tables" :key="t" size="small" class="col-tag">{{ t }}</el-tag>
            </div>
          </div>
          <div v-if="r._result" class="lineage-result">
            <pre>{{ r._result }}</pre>
          </div>
          <div v-if="r._error" class="lineage-error">{{ r._error }}</div>
        </div>
      </template>
    </el-drawer>

    <!-- KPI 指标卡 -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col v-for="k in board?.kpis || []" :key="k.label" :xs="12" :sm="6">
        <div class="stat-card">
          <div class="num">
            {{ k.value }}
            <span v-if="k.unit" class="unit">{{ k.unit }}</span>
          </div>
          <div class="lbl">{{ k.label }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 图表区(点击品类联动) -->
    <el-row :gutter="16">
      <el-col v-for="c in charts" :key="c.id" :span="c.type === 'pie' ? 8 : c.type === 'bar' ? 16 : 24" style="margin-bottom: 16px">
        <div class="panel-card">
          <div class="chart-title">{{ c.title }}</div>
          <div :ref="(el) => setChartEl(c.id, el)" class="chart" />
        </div>
      </el-col>
    </el-row>

    <!-- 明细表(下钻目标) -->
    <div class="panel-card">
      <div class="chart-title">
        数据明细
        <span v-if="activeCategory" class="drill-hint">（已下钻: {{ activeCategory }}）</span>
        <span v-else class="drill-hint">（点击上方图表品类可下钻查看明细）</span>
      </div>

      <!-- 二级品类下钻明细(位于数据明细区内, 下钻时显示) -->
      <div v-if="drillData" class="drill-section" v-loading="drillLoading">
        <div class="drill-title">
          📂 {{ activeCategory }} {{ drillData.label || '二级品类' }}明细（{{ drillData.rows.length }} 条 · 动态下钻查询, 可溯源）
        </div>
        <el-table :data="drillTableData" stripe size="small" max-height="260">
          <el-table-column prop="c0" :label="(drillData.columns || [])[0] || '子品类'" min-width="140" />
          <el-table-column prop="c1" :label="(drillData.columns || [])[1] || '近7天销售额'" min-width="180">
            <template #default="{ row }"><span class="num-cell">{{ fmtCell(row.c1) }}</span></template>
          </el-table-column>
        </el-table>
      </div>

      <el-table ref="tableRef" :data="tableData" stripe size="small" max-height="520" row-key="c0" @expand-change="onExpand" :empty-text="activeCategory ? '该品类暂无明细' : '暂无数据'">
        <!-- 展开行: 二级品类下钻明细, 位于上一级品类行的正下方 -->
        <el-table-column type="expand" width="36">
          <template #default="{ row }">
            <div class="drill-inside">
              <div class="drill-title">
                📂 {{ row.c0 }} {{ drillCache[row.c0]?.label || '二级品类' }}明细
                <span class="drill-hint">（{{ (drillCache[row.c0]?.rows || []).length }} 条 · 动态下钻查询, 可溯源）</span>
              </div>
              <el-table v-if="drillCache[row.c0]" :data="drillRows(row.c0)" stripe size="small" max-height="260" v-loading="drillLoadingKey === row.c0">
                <el-table-column v-for="(col, j) in (drillCache[row.c0].columns || [])" :key="col" :prop="'c' + j" :label="col" min-width="130">
                  <template #default="{ row: dr }">
                    <span :class="{ 'num-cell': typeof dr['c' + j] === 'number' }">{{ fmtCell(dr['c' + j]) }}</span>
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="drill-loading">{{ drillCache[row.c0]?.label || '二级品类' }}明细加载中…</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column
          v-for="(col, i) in board?.table?.columns || []"
          :key="col"
          :prop="'c' + i"
          :label="col"
          min-width="120"
        >
          <template #default="{ row }">
            <span :class="{ 'num-cell': typeof row['c' + i] === 'number' }">{{ fmtCell(row['c' + i]) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 血缘溯源 -->
    <div v-if="board?.lineage" class="lineage-bar">
      📌 数据来源: <b>{{ board.lineage.tables.join(', ') }}</b>
      · 返回 {{ board.lineage.rows }} 行 · 执行 {{ board.lineage.count }} 次
      · 最近耗时 {{ (board.lineage.duration_ms / 1000).toFixed(1) }}s
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { DataAnalysis } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchBoard, fetchDrill, fetchLineage, rerunQueryRun } from '@/api'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId

// 返回上一页(有历史则回退, 无历史兜底回数据分析页)
function goBack() {
  if (window.history.state?.back) {
    router.back()
  } else {
    router.push({ path: '/analysis' })
  }
}

const loading = ref(false)
const board = ref(null)
const activeCategory = ref(null)

// 二级品类下钻: 展开行模式(位于上一级品类行正下方), 按品类缓存下钻数据
const tableRef = ref()
const drillCache = reactive({})
const drillLoadingKey = ref(null)

function drillRows(key) {
  return (drillCache[key]?.rows || []).map((r) => ({
    c0: r[0], c1: r[1], c2: r[2], c3: r[3]
  }))
}

// 展开行事件: 首次展开时动态查询该一级品类的二级品类明细
async function onExpand(row, expanded) {
  if (!expanded) return
  const key = String(row.c0)
  if (drillCache[key]) return
  drillLoadingKey.value = key
  try {
    drillCache[key] = await fetchDrill(taskId, { value: key })
  } catch (e) {
    drillCache[key] = { rows: [] }
    ElMessage.warning('二级品类明细加载失败: ' + (e.response?.data?.detail || e.message || '未知错误'))
  } finally {
    drillLoadingKey.value = null
  }
}

// 图表点击后自动展开对应一级品类的下钻行
function expandRow(name) {
  const row = tableData.value.find((r) => String(r.c0) === name)
  if (row && tableRef.value) tableRef.value.toggleRowExpansion(row, true)
}

// 图表实例管理
const chartEls = {}
const chartInstances = {}

function setChartEl(id, el) {
  if (el) chartEls[id] = el
}

const charts = computed(() => board.value?.charts || [])

// 明细表: 按当前下钻品类过滤(第一列 = 品类)
const filteredRows = computed(() => {
  const rows = board.value?.table?.rows || []
  if (!activeCategory.value) return rows
  return rows.filter((r) => String(r[0]) === activeCategory.value)
})

// el-table 需要对象行(prop 取字段); 数组行需映射为 { c0: ..., c1: ... }
const tableData = computed(() =>
  filteredRows.value.map((r) => Object.fromEntries(r.map((v, i) => ['c' + i, v])))
)

// 图表数据: 联动过滤(柱/饼高亮选中, 数据保留全量; 明细表承载下钻)
function chartData(c) {
  return c.data || []
}

function fmtCell(v) {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'number') return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  return String(v)
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}

// 构建 ECharts option
function buildOption(c) {
  const data = chartData(c)
  const labels = data.map((d) => d.label)
  const values = data.map((d) => d.value)
  const fmtAxis = (v) => {
    const f = Number(v)
    if (Math.abs(f) >= 1e8) return (f / 1e8).toFixed(1) + '亿'
    if (Math.abs(f) >= 1e4) return (f / 1e4).toFixed(0) + '万'
    return String(f)
  }
  if (c.type === 'pie') {
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 11 } },
      series: [{
        type: 'pie', radius: ['38%', '62%'], center: ['50%', '46%'],
        data: data.map((d) => ({ name: d.label, value: d.value })),
        label: { fontSize: 11, formatter: '{b}\n{d}%' },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,0.3)' } }
      }]
    }
  }
  if (c.type === 'line') {
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 56, right: 20, top: 30, bottom: 40 },
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: fmtAxis } },
      series: [{ type: 'line', data: values, smooth: true, symbolSize: 7, itemStyle: { color: '#4f46e5' }, areaStyle: { opacity: 0.12 } }]
    }
  }
  // bar
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 64, right: 20, top: 30, bottom: 48 },
    xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11, rotate: labels.length > 6 ? 30 : 0 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: fmtAxis } },
    series: [{
      type: 'bar', data: values, barMaxWidth: 44,
      itemStyle: { color: '#4f46e5', borderRadius: [4, 4, 0, 0] },
      label: { show: true, position: 'top', fontSize: 10, formatter: (p) => fmtAxis(p.value) }
    }]
  }
}

// 渲染单张图
function renderChart(c) {
  const el = chartEls[c.id]
  if (!el) return
  if (chartInstances[c.id]) chartInstances[c.id].dispose()
  const chart = echarts.init(el)
  chartInstances[c.id] = chart
  chart.setOption(buildOption(c))

// 图表点击品类 -> 下钻联动(明细过滤 + 自动展开二级品类明细 + 其他图高亮)
  if (c.dim) {
    chart.on('click', (params) => {
      const name = params?.name
      if (!name) return
      activeCategory.value = name
      expandRow(name)
      syncHighlight()
    })
  }
}

// 联动高亮: 其他品类图高亮当前选中品类
function syncHighlight() {
  charts.value.forEach((c) => {
    const chart = chartInstances[c.id]
    if (!chart || !c.dim) return
    chart.dispatchAction({ type: 'downplay', seriesIndex: 0 })
    if (activeCategory.value) {
      const idx = (c.data || []).findIndex((d) => d.label === activeCategory.value)
      if (idx >= 0) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
    }
  })
}

function clearFilter() {
  activeCategory.value = null
  syncHighlight()
  // 收起所有展开行(二级品类明细)
  if (tableRef.value) {
    tableData.value.forEach((r) => tableRef.value.toggleRowExpansion(r, false))
  }
}

function renderAll() {
  charts.value.forEach((c) => renderChart(c))
  syncHighlight()
}

async function load() {
  loading.value = true
  try {
    board.value = await fetchBoard(taskId)
    await nextTick()
    renderAll()
    window.addEventListener('resize', onResize)
  } catch (e) {
    ElMessage.error('看板加载失败: ' + (e.response?.data?.detail || e.message || '未知错误'))
  } finally {
    loading.value = false
  }
}

function onResize() {
  Object.values(chartInstances).forEach((c) => c && c.resize())
}

onMounted(load)

// ---------- 数据溯源(报告数字核验) ----------
const lineageVisible = ref(false)
const lineageLoading = ref(false)
const runs = ref([])

async function openLineage() {
  lineageVisible.value = true
  lineageLoading.value = true
  try {
    const data = await fetchLineage(taskId)
    runs.value = (data.runs || []).map((r) => ({ ...r, _loading: false, _result: '', _error: '' }))
  } catch (e) {
    /* 已提示 */
  } finally {
    lineageLoading.value = false
  }
}

async function rerun(r) {
  r._loading = true
  r._result = ''
  r._error = ''
  try {
    const data = await rerunQueryRun(taskId, r.id)
    if (data.ok) {
      r._result = `共 ${data.row_count} 行, SQL:\n${data.sql_text}\n\n${(data.sample || []).join('\n')}`
    } else {
      r._error = data.error || '重跑失败'
    }
  } catch (e) {
    r._error = '重跑失败(权限或执行错误)'
  } finally {
    r._loading = false
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  Object.values(chartInstances).forEach((c) => c && c.dispose())
})
</script>

<style scoped>
.sub { color: #6b7280; font-size: 13px; margin-top: 4px; }
.filter-bar { margin-bottom: 14px; }
.stat-card {
  border: 1px solid #eceff5; border-radius: 10px; background: #fafbfc;
  padding: 14px 16px; height: 100%;
}
.stat-card .num { font-size: 20px; font-weight: 700; color: #1f2937; }
.stat-card .unit { font-size: 12px; font-weight: 400; color: #9ca3af; margin-left: 2px; }
.stat-card .lbl { font-size: 12px; color: #6b7280; margin-top: 4px; }
.panel-card {
  border: 1px solid #eceff5; border-radius: 10px; background: #fff; padding: 14px;
}
.chart-title { font-size: 14px; font-weight: 600; color: #374151; margin-bottom: 8px; }
.chart { height: 320px; }
.drill-hint { font-size: 12px; font-weight: 400; color: #9ca3af; margin-left: 6px; }
.drill-inside {
  border: 1px dashed #c7d2fe; background: #f5f7ff; border-radius: 8px;
  padding: 10px 12px; margin: 4px 0;
}
.drill-title { font-size: 13px; font-weight: 600; color: #4f46e5; margin-bottom: 8px; }
.drill-loading { font-size: 13px; color: #9ca3af; padding: 10px; }
.num-cell { font-variant-numeric: tabular-nums; }
.lineage-bar {
  margin-top: 14px; font-size: 12px; color: #6b7280;
  background: #f3f4f6; border-radius: 8px; padding: 8px 12px;
}

/* 数据溯源抽屉 */
.lineage-item {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;
}
.lineage-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.lineage-sql pre {
  font-family: 'JetBrains Mono', Consolas, monospace; font-size: 11px; color: #374151;
  background: #f9fafb; border-radius: 6px; padding: 8px; margin: 8px 0 4px;
  white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow: auto;
}
.lineage-tables { font-size: 12px; color: #6b7280; }
.col-tag { margin-right: 4px; }
.lineage-result pre {
  font-family: 'JetBrains Mono', Consolas, monospace; font-size: 11px;
  background: #ecfdf5; color: #065f46; border-radius: 6px; padding: 8px; margin-top: 8px;
  white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow: auto;
}
.lineage-error { color: #dc2626; font-size: 12px; margin-top: 8px; }
</style>
