<template>
  <div>
    <div class="page-header">
      <h2>🖊️ 审批中心</h2>
      <p>处理等待人工审批的任务(敏感表查询 / 大结果集 / 成本超限)与定时任务永久审批</p>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- 待审批任务 -->
      <el-tab-pane label="待审批任务" name="pending">
        <div v-if="loading" v-loading="true" style="min-height: 200px" />

        <el-empty v-else-if="!tasks.length" description="暂无待审批任务 🎉" />

        <div v-else>
          <el-alert
            :title="`共 ${tasks.length} 个任务等待审批`"
            type="info"
            :closable="false"
            show-icon
            style="margin-bottom: 14px"
          />
          <el-card v-for="t in tasks" :key="t.task_id" class="task-card">
            <div class="task-info">
              <el-tag type="warning" size="small">待审批</el-tag>
              <span class="q">{{ t.query }}</span>
            </div>
            <div class="task-meta">
              <span>任务 ID: <code>{{ t.task_id }}</code></span>
              <span v-if="t.progress_detail">· {{ t.progress_detail }}</span>
              <span>· {{ fmtTime(t.created_at) }}</span>
            </div>
            <el-divider style="margin: 12px 0" />
            <div class="approve-row">
          <el-input
            v-model="t._comment"
            placeholder="审批意见(可选)"
            clearable
            style="max-width: 340px"
          />
          <el-button type="success" :loading="t._loading" @click="onApprove(t, true)">✅ 批准</el-button>
          <el-button type="danger" :loading="t._loading" @click="onApprove(t, false)">❌ 拒绝</el-button>
        </div>
      </el-card>
    </div>
      </el-tab-pane>

      <!-- 定时任务永久审批(仅管理员) -->
      <el-tab-pane v-if="store.isAdmin" label="定时任务永久审批" name="permanent">
        <div v-if="schedLoading" v-loading="true" style="min-height: 200px" />
        <template v-else>
          <el-alert
            title="定时任务触发的分析不会逐单挂起审批; 在此对定时任务做一次性「永久审批」留痕。通过后不再提示; 拒绝后任务停用。"
            type="info"
            :closable="false"
            show-icon
            style="margin-bottom: 14px"
          />
          <el-empty v-if="!scheduled.length" description="暂无定时任务" />
          <el-card v-for="s in scheduled" :key="s.id" class="task-card">
            <div class="task-info">
              <el-tag size="small" :type="approvalTagType(s.approval_status)">{{ approvalText(s) }}</el-tag>
              <span class="q">{{ s.name }}</span>
            </div>
            <div class="task-meta">
              <span>需求: {{ s.query }}</span>
              <span>· 频率: {{ s.cron_desc || s.cron }}</span>
              <span>· 创建人: {{ s.owner }}</span>
            </div>
            <el-divider style="margin: 12px 0" />
            <div class="approve-row" v-if="s.approval_status !== 'approved'">
              <el-button type="success" :loading="s._loading" @click="onPermanent(s, true)">✅ 永久批准</el-button>
              <el-button type="danger" :loading="s._loading" @click="onPermanent(s, false)">❌ 永久拒绝</el-button>
            </div>
            <div class="task-meta" v-else>
              <span>✅ 已永久批准 · 审批人 {{ s.approved_by }} · {{ fmtTime(s.approved_at) }}</span>
            </div>
          </el-card>
        </template>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { listPendingApprovals, approveTask, listScheduledTasks, permanentApproval } from '@/api'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
const tasks = ref([])
const loading = ref(true)
const activeTab = ref('pending')
const scheduled = ref([])
const schedLoading = ref(false)
let timer = null

function fmtTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

function approvalText(s) {
  return { pending: '待永久审批', approved: '已永久批准', rejected: '已拒绝' }[s.approval_status] || s.approval_status
}
function approvalTagType(st) {
  return st === 'approved' ? 'success' : st === 'rejected' ? 'danger' : 'warning'
}

async function loadScheduled() {
  schedLoading.value = true
  try {
    const data = await listScheduledTasks()
    scheduled.value = (data.tasks || []).map((s) => ({ ...s, _loading: false }))
  } catch (e) {
    /* 已提示 */
  } finally {
    schedLoading.value = false
  }
}

async function onPermanent(s, approved) {
  s._loading = true
  try {
    await permanentApproval(s.id, { approved })
    ElMessage.success(approved ? `「${s.name}」已永久批准, 后续触发不再提示` : `「${s.name}」已拒绝并停用`)
    loadScheduled()
  } catch (e) {
    /* 已提示 */
  } finally {
    s._loading = false
  }
}

function onTabChange(name) {
  if (name === 'permanent') loadScheduled()
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const data = await listPendingApprovals()
    // 保留已填写的审批意见: 5s 轮询重建对象会清空正在输入的 _comment(回归根因)
    const prevMap = new Map(tasks.value.map((x) => [x.task_id, x]))
    tasks.value = (data.tasks || []).map((t) => ({
      ...t,
      _comment: prevMap.get(t.task_id)?._comment || '',
      _loading: false,
    }))
  } catch (e) {
    /* 已提示 */
  } finally {
    if (!silent) loading.value = false
  }
}

async function onApprove(t, approved) {
  t._loading = true
  try {
    await approveTask(t.task_id, {
      approved,
      approver: store.username || 'approver',
      comment: (t._comment || '').trim()
    })
    ElMessage.success(approved ? `任务 ${t.task_id.slice(0, 8)} 已批准` : `任务 ${t.task_id.slice(0, 8)} 已拒绝`)
    load()
  } catch (e) {
    /* 已提示 */
  } finally {
    t._loading = false
  }
}

onMounted(() => {
  load()
  // 自动轮询: 新审批请求到达后自动出现在列表(每 5s 静默刷新)
  timer = setInterval(() => load(true), 5000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.task-card { margin-bottom: 14px; border-radius: 12px; }
.task-info { display: flex; align-items: center; gap: 10px; }
.task-info .q { font-size: 15px; font-weight: 600; }
.task-meta { margin-top: 6px; color: #6b7280; font-size: 13px; display: flex; gap: 8px; flex-wrap: wrap; }
.task-meta code { background: #f3f4f6; padding: 1px 6px; border-radius: 4px; }
.approve-row { display: flex; gap: 10px; align-items: center; }
</style>
