<template>
  <div>
    <div class="page-header">
      <h2>📋 任务历史</h2>
      <p>查看全部历史任务及状态,可下载已完成的报告</p>
    </div>

    <div class="panel-card">
      <!-- 状态筛选 + 搜索 -->
      <div style="display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; align-items: center">
        <el-radio-group v-model="status" @change="reload">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="completed">已完成</el-radio-button>
          <el-radio-button value="running">运行中</el-radio-button>
          <el-radio-button value="awaiting_approval">待审批</el-radio-button>
          <el-radio-button value="failed">失败</el-radio-button>
          <el-radio-button value="canceled">已取消</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="keyword"
          placeholder="搜索我提交的任务(按需求内容)..."
          clearable
          style="width: 280px"
          @keyup.enter="reload"
          @clear="reload"
        >
          <template #prefix><span style="color: #9ca3af">🔍</span></template>
        </el-input>
        <el-date-picker
          v-model="startDate"
          type="date"
          placeholder="开始日期"
          value-format="YYYY-MM-DD"
          :disabled-date="disableStartDate"
          clearable
          style="width: 150px"
          @change="reload"
          @clear="reload"
        />
        <span style="color: #9ca3af">至</span>
        <el-date-picker
          v-model="endDate"
          type="date"
          placeholder="结束日期"
          value-format="YYYY-MM-DD"
          :disabled-date="disableEndDate"
          clearable
          style="width: 150px"
          @change="reload"
          @clear="reload"
        />
        <el-button type="primary" @click="reload">搜索</el-button>
      </div>

      <el-table v-loading="loading" :data="tasks" stripe>
        <el-table-column prop="task_id" label="任务 ID" width="230">
          <template #default="{ row }"><code class="tid">{{ row.task_id }}</code></template>
        </el-table-column>
        <el-table-column prop="query" label="需求" min-width="260" show-overflow-tooltip />
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <router-link v-if="row.has_board" :to="`/board/${row.task_id}`">
              <el-button link type="success">📊 看板</el-button>
            </router-link>
            <el-button v-if="row.has_pdf" link type="primary" @click="handleDownload(row.task_id)">
              📄 下载
            </el-button>
            <span v-if="!row.has_pdf && !row.has_board" style="color: #9ca3af; font-size: 13px">—</span>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next, total"
        style="margin-top: 14px; justify-content: flex-end"
        @current-change="load"
      />
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { listTasks } from '@/api'
import { handleDownload } from '@/utils/download'

const tasks = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const status = ref('')
const keyword = ref('')
const startDate = ref(null)
const endDate = ref(null)
const loading = ref(false)

// 日期选择限制: 今天及以前可选, 未来日期置灰不可点击
function startOfToday() {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

function disableStartDate(date) {
  return date.getTime() > startOfToday().getTime()
}

function disableEndDate(date) {
  if (date.getTime() > startOfToday().getTime()) return true
  // 结束日期不早于开始日期(已选开始日期时)
  if (startDate.value) {
    const s = new Date(startDate.value + 'T00:00:00')
    return date.getTime() < s.getTime()
  }
  return false
}

function reload() {
  page.value = 1
  load()
}

async function load() {
  loading.value = true
  try {
    const data = await listTasks({
      status: status.value || undefined,
      keyword: keyword.value?.trim() || undefined,
      date_from: startDate.value || undefined,
      date_to: endDate.value || undefined,
      page: page.value,
      page_size: pageSize
    })
    tasks.value = data.tasks || []
    total.value = data.total || 0
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

function statusType(s) {
  return { completed: 'success', failed: 'danger', canceled: 'info', awaiting_approval: 'warning', running: 'primary' }[s] || 'primary'
}
function statusText(s) {
  return { completed: '已完成', failed: '失败', canceled: '已取消', awaiting_approval: '待审批', running: '运行中', pending: '排队中' }[s] || s
}
function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}

onMounted(load)
</script>

<style scoped>
.tid { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
</style>
