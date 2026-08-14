<template>
  <div>
    <div class="page-header">
      <h2>🗒️ 操作日志</h2>
      <p>任务操作记录(提交 / 审批 / 执行结果), 点击"详情"查看完整时间线</p>
    </div>

    <div class="panel-card">
      <!-- 搜索 + 筛选工具条 -->
      <div class="toolbar">
        <el-input
          v-model="keyword"
          placeholder="搜索提交者 / 操作内容 / 审批者 / 备注"
          clearable
          style="width: 300px"
          @keyup.enter="doSearch"
          @clear="doSearch"
        />
        <el-button type="primary" @click="doSearch">搜索</el-button>
        <el-button @click="filterDialog = true">          筛选
          <el-badge v-if="activeFilterCount" :value="activeFilterCount" type="danger" class="filter-badge" />
        </el-button>
        <el-button v-if="activeFilterCount" @click="resetFilters">重置</el-button>
        <el-button type="success" plain :icon="Download" @click="onExport">导出 CSV</el-button>
        <span v-if="activeFilterCount" class="filter-summary">已应用 {{ activeFilterCount }} 项筛选, 未选择的条件默认全部</span>
      </div>

      <el-table v-loading="loading" :data="logs" stripe>
        <el-table-column label="提交时间" width="155">
          <template #default="{ row }">{{ fmtTime(row.submitted_at) }}</template>
        </el-table-column>
        <el-table-column prop="submitted_by" label="提交者" width="100">
          <template #default="{ row }"><span>{{ row.submitted_by || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="操作内容(任务)" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="op-text">{{ row.query }}</span>
          </template>
        </el-table-column>
        <el-table-column label="审批者" width="100">
          <template #default="{ row }">
            <span>{{ row.approver || (row.approval_result === 'pending' ? '待审批' : '-') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="审批结果" width="110">
          <template #default="{ row }">
            <el-tag :type="approvalTag(row.approval_result)" size="small" effect="light">
              {{ approvalText(row.approval_result) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="备注" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.approval_comment || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="执行结果" width="120">
          <template #default="{ row }">
            <el-tooltip v-if="row.result === 'failed'" :content="row.error || ''" placement="top">
              <el-tag type="danger" size="small" effect="light">失败</el-tag>
            </el-tooltip>
            <el-tag v-else :type="resultTag(row.result)" size="small" effect="light">
              {{ resultText(row.result) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
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

    <!-- 筛选弹窗: 各维度独立, 不选默认全部 -->
    <el-dialog v-model="filterDialog" title="筛选操作日志" width="480px">
      <el-form label-width="80px">
        <el-form-item label="提交时间">
          <el-date-picker
            v-model="filter.dateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="提交者">
          <el-input v-model="filter.submittedBy" placeholder="留空 = 全部提交者" clearable />
        </el-form-item>
        <el-form-item label="审批人">
          <el-input v-model="filter.approver" placeholder="留空 = 全部审批人" clearable />
        </el-form-item>
        <el-form-item label="审批结果">
          <el-select v-model="filter.approvalResult" placeholder="全部" clearable style="width: 100%">
            <el-option label="已通过" value="approved" />
            <el-option label="已拒绝" value="rejected" />
            <el-option label="待审批" value="pending" />
            <el-option label="无需审批" value="none" />
          </el-select>
        </el-form-item>
        <el-form-item label="执行结果">
          <el-select v-model="filter.result" placeholder="全部" clearable style="width: 100%">
            <el-option label="执行完成" value="completed" />
            <el-option label="执行失败" value="failed" />
            <el-option label="已取消" value="canceled" />
            <el-option label="执行中" value="running" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="filter-tip">未选择的筛选条件默认全部(不限制); 各条件可组合批量筛选</div>
      <template #footer>
        <el-button @click="resetFilters">重置</el-button>
        <el-button type="primary" @click="applyFilters">应用筛选</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉: 任务完整时间线 -->
    <el-drawer v-model="drawer" :title="`任务详情 · ${current?.query?.slice(0, 30) || ''}`" size="46%">
      <template v-if="current">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="提交者">{{ current.submitted_by || '-' }}</el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ fmtTime(current.submitted_at) }}</el-descriptions-item>
          <el-descriptions-item label="审批者">{{ current.approver || '-' }}</el-descriptions-item>
          <el-descriptions-item label="审批结果">{{ approvalText(current.approval_result) }}</el-descriptions-item>
          <el-descriptions-item label="备注" :span="2">{{ current.approval_comment || '-' }}</el-descriptions-item>
          <el-descriptions-item label="执行结果">{{ resultText(current.result) }}</el-descriptions-item>
          <el-descriptions-item label="任务 ID">
            <code class="tid">{{ current.task_id }}</code>
          </el-descriptions-item>
          <el-descriptions-item v-if="current.result === 'failed'" label="失败原因" :span="2">
            <span class="err">{{ current.error || '-' }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <h4 style="margin: 4px 0 10px">⏱️ 时间线</h4>
        <el-timeline v-loading="eventsLoading">
          <el-timeline-item
            v-for="ev in events"
            :key="ev.id"
            :timestamp="fmtTime(ev.created_at)"
            :type="timelineType(ev.event)"
            placement="top"
          >
            <b>{{ eventText(ev.event) }}</b>
            <div class="ev-detail">{{ describeEvent(ev) }}</div>
          </el-timeline-item>
          <el-empty v-if="!eventsLoading && events.length === 0" description="暂无审计事件" />
        </el-timeline>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listAuditLogs, listTaskEvents, exportAuditLogs } from '@/api'

const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(false)

// 搜索 + 筛选状态
const keyword = ref('')
const filterDialog = ref(false)
const filter = reactive({
  dateRange: [], // [YYYY-MM-DD, YYYY-MM-DD]
  submittedBy: '',
  approver: '',
  approvalResult: '', // approved/rejected/pending/none
  result: '' // completed/failed/canceled/running
})
const activeFilterCount = computed(() => {
  let n = 0
  if (keyword.value.trim()) n++
  if (filter.dateRange && filter.dateRange.length === 2) n++
  if (filter.submittedBy.trim()) n++
  if (filter.approver.trim()) n++
  if (filter.approvalResult) n++
  if (filter.result) n++
  return n
})

const drawer = ref(false)
const current = ref(null)
const events = ref([])
const eventsLoading = ref(false)

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}

// ---- 审批结果 ----
function approvalText(v) {
  return { approved: '已通过', rejected: '已拒绝', pending: '待审批', none: '无需审批' }[v] || '-'
}
function approvalTag(v) {
  return { approved: 'success', rejected: 'danger', pending: 'warning', none: 'info' }[v] || 'info'
}

// ---- 执行结果 ----
function resultText(v) {
  return { completed: '执行完成', failed: '执行失败', canceled: '已取消', running: '执行中' }[v] || v || '-'
}
function resultTag(v) {
  return { completed: 'success', failed: 'danger', canceled: 'info', running: 'primary' }[v] || 'info'
}

// ---- 事件(时间线用) ----
const EVENT_TEXT = {
  task_submitted: '提交任务',
  pipeline_started: '流水线启动',
  awaiting_approval: '进入审批队列',
  approved: '审批通过',
  rejected: '审批拒绝',
  approval_timeout: '审批超时',
  pipeline_finished: '执行结束',
  task_canceled: '任务取消'
}
function eventText(ev) {
  return EVENT_TEXT[ev] || ev
}
function timelineType(ev) {
  if (ev === 'approved') return 'success'
  if (ev === 'rejected' || ev === 'task_canceled') return 'danger'
  if (ev === 'awaiting_approval') return 'warning'
  return 'primary'
}
function describeEvent(ev) {
  const who = ev.actor && ev.actor !== 'system' ? ev.actor : '系统'
  switch (ev.event) {
    case 'task_submitted': return `${who} 提交了任务`
    case 'pipeline_started': return '流水线开始执行'
    case 'awaiting_approval': return '任务等待人工审批'
    case 'approved': return `${who} 审批通过${ev.detail?.comment ? `, 意见: ${ev.detail.comment}` : ''}`
    case 'rejected': return `${who} 拒绝审批${ev.detail?.comment ? `, 意见: ${ev.detail.comment}` : ''}`
    case 'approval_timeout': return '审批超时, 系统自动处理'
    case 'pipeline_finished': return ev.detail?.status === 'failed' ? '任务执行失败' : '任务执行完成'
    case 'task_canceled': return `${who} 取消了任务`
    default: return ev.detail || ''
  }
}

async function openDetail(row) {
  current.value = row
  drawer.value = true
  eventsLoading.value = true
  events.value = []
  try {
    const data = await listTaskEvents(row.task_id)
    events.value = data || []
  } catch (e) {
    /* 已提示 */
  } finally {
    eventsLoading.value = false
  }
}

function buildParams() {
  const params = { page: page.value, page_size: pageSize }
  if (keyword.value.trim()) params.keyword = keyword.value.trim()
  if (filter.dateRange && filter.dateRange.length === 2) {
    params.date_from = filter.dateRange[0]
    params.date_to = filter.dateRange[1]
  }
  if (filter.submittedBy.trim()) params.submitted_by = filter.submittedBy.trim()
  if (filter.approver.trim()) params.approver = filter.approver.trim()
  if (filter.approvalResult) params.approval_result = filter.approvalResult
  if (filter.result) params.result = filter.result
  return params
}

async function load() {
  loading.value = true
  try {
    const data = await listAuditLogs(buildParams())
    logs.value = data.logs || []
    total.value = data.total || 0
  } catch (e) {
    /* 已提示 */
  } finally {
    loading.value = false
  }
}

// ---- 导出 CSV(审计合规: append-only, 导出即归档) ----
async function onExport() {
  try {
    const resp = await exportAuditLogs(buildParams())
    const blob = resp.data
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-logs-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    if (!e.response) ElMessage.error(e.message || '导出失败')
  }
}

// ---- 搜索 / 筛选 ----
function doSearch() {
  page.value = 1
  load()
}
function applyFilters() {
  filterDialog.value = false
  doSearch()
}
function resetFilters() {
  keyword.value = ''
  Object.assign(filter, {
    dateRange: [],
    submittedBy: '',
    approver: '',
    approvalResult: '',
    result: ''
  })
  doSearch()
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.filter-badge {
  margin-left: 2px;
}
.filter-summary {
  font-size: 12px;
  color: #8a919f;
}
.filter-tip {
  font-size: 12px;
  color: #9ca3af;
}
.tid { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.op-text { font-size: 13px; }
.err { color: #dc2626; font-size: 12px; }
.ev-detail { font-size: 12px; color: #6b7280; margin-top: 2px; }
</style>
