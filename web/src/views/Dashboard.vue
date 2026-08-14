<template>
  <div>
    <div class="page-header">
      <h2>🏠 工作台</h2>
      <p>欢迎回来,{{ store.username }}!这里是平台运行概览</p>
    </div>

    <!-- 快捷入口 + 趋势 -->
    <el-row :gutter="16">
      <el-col :span="10">
        <div class="panel-card">
          <h3 style="margin-top: 0">⚡ 快捷入口</h3>
          <div class="quick-grid">
            <div v-for="q in quickLinks" :key="q.path" class="quick-item" @click="$router.push(q.path)">
              <div class="qi-icon">{{ q.icon }}</div>
              <div class="qi-title">{{ q.title }}</div>
              <div class="qi-desc">{{ q.desc }}</div>
            </div>
          </div>
        </div>
      </el-col>
      <el-col :span="14">
        <div class="panel-card">
          <div ref="trendEl" class="chart" />
        </div>
      </el-col>
    </el-row>

    <!-- 最新任务 -->
    <div class="panel-card" style="margin-top: 16px">
      <div style="display: flex; justify-content: space-between; align-items: center">
        <h3 style="margin: 0">🕐 最新任务</h3>
        <el-button link type="primary" @click="$router.push('/tasks')">查看全部 →</el-button>
      </div>
      <el-table :data="latest" stripe style="margin-top: 10px">
        <el-table-column prop="task_id" label="任务 ID" width="240">
          <template #default="{ row }"><code class="tid">{{ row.task_id.slice(0, 18) }}…</code></template>
        </el-table-column>
        <el-table-column prop="query" label="需求" min-width="260" show-overflow-tooltip />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { fetchDashboard } from '@/api'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
const trendEl = ref()
let trendChart = null

const data = ref({ task_total: 0, completion_rate: 0, cost_actual: 0, node_runs: 0, node_retries: 0, user_count: 0, trend_7d: [], latest_tasks: [] })
const latest = computed(() => data.value.latest_tasks || [])

const quickLinks = computed(() => {
  const links = []
  if (store.isAdmin) {
    // 管理员无数据分析页(访问会被重定向到用户管理), 快捷入口直接用用户管理
    links.push({ path: '/admin/users', icon: '👥', title: '用户管理', desc: '管理平台用户与权限' })
  } else {
    links.push({ path: '/analysis', icon: '📊', title: '数据分析', desc: '提交自然语言分析需求' })
  }
  links.push({ path: '/tasks', icon: '📋', title: '任务历史', desc: '查看全部历史任务' })
  if (store.isAdmin || store.roles.includes('approver')) {
    links.push({ path: '/approvals', icon: '🖊️', title: '审批中心', desc: '处理待审批任务' })
  }
  if (store.isAdmin) {
    links.push({ path: '/admin/metrics', icon: '📈', title: '指标看板', desc: '运行指标与成本' })
  }
  return links
})

function statusType(s) {
  return { completed: 'success', failed: 'danger', canceled: 'info', awaiting_approval: 'warning' }[s] || 'primary'
}
function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}

function renderTrend() {
  const trend = data.value.trend_7d || []
  trendChart = echarts.init(trendEl.value)
  trendChart.setOption({
    title: { text: '近 7 天任务数', left: 4, textStyle: { fontSize: 15, fontWeight: 700 } },
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 44, bottom: 28 },
    xAxis: { type: 'category', data: trend.map((t) => t.date) },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'bar', data: trend.map((t) => t.count),
      itemStyle: { color: '#4f46e5', borderRadius: [6, 6, 0, 0] }, barWidth: 28
    }]
  })
}

async function load() {
  try {
    data.value = await fetchDashboard()
    renderTrend()
    window.addEventListener('resize', () => trendChart?.resize())
  } catch (e) {
    /* 已提示 */
  }
}

onMounted(load)
onBeforeUnmount(() => trendChart?.dispose())
</script>

<style scoped>
.chart { height: 260px; }
.quick-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.quick-item {
  border: 1px solid #eceff5; border-radius: 12px; padding: 14px;
  cursor: pointer; transition: all 0.15s ease; background: #fafbfc;
}
.quick-item:hover { border-color: #4f46e5; box-shadow: 0 2px 8px rgba(79, 70, 229, 0.1); transform: translateY(-2px); }
.qi-icon { font-size: 22px; }
.qi-title { font-weight: 700; margin-top: 6px; }
.qi-desc { font-size: 12px; color: #6b7280; margin-top: 2px; }
.tid { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
</style>
