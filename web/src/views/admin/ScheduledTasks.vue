<template>
  <div>
    <div class="page-header">
      <h2>⏰ 定时任务</h2>
      <p>按 cron 定期自动执行分析(如每天早上 9 点跑销售日报),结果以站内通知推送给创建人。调度器随应用启动,单机部署生效。</p>
    </div>

    <div class="panel-card">
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">新建定时任务</el-button>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column label="执行频率" min-width="150">
          <template #default="{ row }">
            <el-tag size="small" type="success">{{ row.cron_desc || row.cron }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分析需求" min-width="240">
          <template #default="{ row }"><span class="query">{{ row.query }}</span></template>
        </el-table-column>
        <el-table-column label="数据源" min-width="140">
          <template #default="{ row }">
            <template v-if="!row.data_source_ids || !row.data_source_ids.length">主库</template>
            <el-tag v-for="id in row.data_source_ids" :key="id" size="small" class="col-tag">{{ dsName(id) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="owner" label="创建人" width="90" />
        <el-table-column label="最近运行" width="170">
          <template #default="{ row }">{{ fmtTime(row.last_run_at) || '-' }}</template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch :model-value="!!row.enabled" size="small" @change="(v) => toggleEnabled(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑定时任务' : '新建定时任务'" width="620px" destroy-on-close>
      <el-form :model="form" label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如 每日销售日报" />
        </el-form-item>
        <el-form-item label="分析需求" required>
          <el-input v-model="form.query" type="textarea" :rows="3"
            placeholder="如 统计最近7天各品类销售额,对比上周变化" />
        </el-form-item>
        <el-form-item label="执行频率" required>
          <div class="freq-row">
            <el-select v-model="form.schedule_type" style="width: 130px" @change="onTypeChange">
              <el-option label="每天" value="daily" />
              <el-option label="每周" value="weekly" />
              <el-option label="每月" value="monthly" />
              <el-option label="自定义(cron)" value="custom" />
            </el-select>
            <el-time-select v-if="form.schedule_type !== 'custom'" v-model="form.schedule_time"
              start="00:00" step="00:30" end="23:30" placeholder="执行时间" style="width: 130px" />
            <el-select v-if="form.schedule_type === 'weekly'" v-model="form.schedule_weekday"
              multiple collapse-tags placeholder="选择星期" style="width: 220px">
              <el-option v-for="(label, v) in weekdays" :key="v" :label="label" :value="String(v)" />
            </el-select>
            <el-input-number v-if="form.schedule_type === 'monthly'" v-model="form.schedule_day"
              :min="1" :max="28" style="width: 140px" />
            <span v-if="form.schedule_type === 'monthly'" class="tip">号</span>
            <el-input v-if="form.schedule_type === 'custom'" v-model="form.cron"
              placeholder="0 9 * * *" style="width: 200px" />
          </div>
          <div class="tip" style="margin-top: 6px">
            <template v-if="form.schedule_type === 'daily'">每天 {{ form.schedule_time }} 自动执行</template>
            <template v-else-if="form.schedule_type === 'weekly'">
              每周 {{ (form.schedule_weekday || []).map(w => weekdays[w]).join('、') || '—' }} {{ form.schedule_time }} 自动执行
            </template>
            <template v-else-if="form.schedule_type === 'monthly'">每月 {{ form.schedule_day }} 号 {{ form.schedule_time }} 自动执行</template>
            <template v-else>高级模式:直接填写 cron 表达式(分 时 日 月 周)</template>
          </div>
        </el-form-item>
        <el-form-item v-if="store.isAdmin" label="数据源">
          <el-select v-model="form.data_source_ids" multiple filterable collapse-tags style="width: 100%"
            placeholder="不选=主库; 可多选跨库执行">
            <el-option v-for="ds in dataSources" :key="ds.id" :label="ds.name" :value="ds.id" />
          </el-select>
          <div class="tip">可多选: 每个选中的库都会各跑一次分析;不选=主库。</div>
        </el-form-item>
        <el-form-item v-if="store.isAdmin" label="通知人员">
          <el-select v-model="form.notify_users" multiple filterable collapse-tags style="width: 100%"
            placeholder="结果通知给谁(不选=仅创建人)">
            <el-option v-for="u in allUsers" :key="u.name" :label="u.name" :value="u.name" />
          </el-select>
          <div class="tip">仅管理员可设置;不选则结果只通知创建人。</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { createScheduledTask, deleteScheduledTask, listDataSources, listScheduledTasks, listUsers, updateScheduledTask } from '@/api'

const store = useUserStore()
const rows = ref([])
const dataSources = ref([])
const allUsers = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)

const emptyForm = () => ({
  id: null, name: '', query: '', schedule_type: 'daily', schedule_time: '09:00',
  schedule_weekday: ['1'], schedule_day: 1, cron: '0 9 * * *',
  data_source_ids: [], notify_users: [], enabled: true
})
const form = reactive(emptyForm())
const weekdays = { 0: '周日', 1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六' }

function onTypeChange() {
  if (form.schedule_type !== 'custom') form.cron = ''
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}

function dsName(id) {
  const ds = dataSources.value.find((x) => x.id === id)
  return ds ? ds.name : `#${id}`
}

async function load() {
  loading.value = true
  try {
    const reqs = [listScheduledTasks()]
    if (store.isAdmin) reqs.push(listDataSources(), listUsers())
    const [t, ...rest] = await Promise.all(reqs)
    rows.value = t.tasks || []
    if (store.isAdmin) {
      dataSources.value = rest[0]?.sources || []
      allUsers.value = (rest[1]?.users || []).map((u) => ({ name: u.name }))
    }
  } catch (e) { /* 已提示 */ } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id, name: row.name, query: row.query,
    schedule_type: row.schedule_type || 'daily',
    schedule_time: row.schedule_time || '09:00',
    schedule_weekday: (row.schedule_weekday || '1').split(',').filter(Boolean),
    schedule_day: row.schedule_day || 1,
    cron: row.cron || '',
    data_source_ids: row.data_source_ids || [],
    notify_users: row.notify_users || [],
    enabled: !!row.enabled
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim() || !form.query.trim()) {
    ElMessage.warning('名称与分析需求必填')
    return
  }
  if (form.schedule_type === 'custom' && !form.cron.trim()) {
    ElMessage.warning('请填写 cron 表达式')
    return
  }
  const payload = {
    name: form.name.trim(),
    query: form.query.trim(),
    schedule_type: form.schedule_type,
    schedule_time: form.schedule_time,
    schedule_weekday: Array.isArray(form.schedule_weekday) ? form.schedule_weekday.join(',') : form.schedule_weekday,
    schedule_day: form.schedule_day,
    cron: form.schedule_type === 'custom' ? form.cron.trim() : undefined,
    data_source_ids: store.isAdmin ? form.data_source_ids : undefined,
    notify_users: store.isAdmin ? form.notify_users : undefined,
    enabled: form.enabled
  }
  saving.value = true
  try {
    if (form.id) {
      await updateScheduledTask(form.id, payload)
      ElMessage.success('已更新(调度已同步)')
    } else {
      await createScheduledTask(payload)
      ElMessage.success('已创建(已纳入调度)')
    }
    dialogVisible.value = false
    load()
  } catch (e) { /* 已提示 */ } finally {
    saving.value = false
  }
}

async function toggleEnabled(row, val) {
  try {
    await updateScheduledTask(row.id, { enabled: val })
    row.enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
  } catch (e) { /* 已提示 */ }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除定时任务「${row.name}」?`, '删除定时任务', { type: 'warning' })
  } catch { return }
  await deleteScheduledTask(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.page-header p { color: #888; font-size: 13px; margin-top: 4px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.tip { color: #999; font-size: 12px; margin-top: 4px; line-height: 1.6; }
.query { color: #374151; }
</style>
