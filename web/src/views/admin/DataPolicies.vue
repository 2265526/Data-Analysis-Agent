<template>
  <div>
    <div class="page-header">
      <h2>🔐 数据权限</h2>
      <p>表级/列级/行级数据访问控制。规则为"限制性声明": 未配置 = 默认允许; 配置后对目标(角色/用户)生效, 用户级优先于角色级。</p>
    </div>

    <div class="panel-card">
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">新建规则</el-button>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
        <span class="tip">列模式: <el-tag size="small" type="success">allow</el-tag> 原样可见 /
          <el-tag size="small" type="warning">mask</el-tag> 脱敏(用掩码表达式) /
          <el-tag size="small" type="danger">deny</el-tag> 禁止访问</span>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column label="目标" width="140">
          <template #default="{ row }">
            <el-tag size="small" :type="row.target_type === 'role' ? 'primary' : 'info'">
              {{ row.target_type === 'role' ? '角色' : '用户' }}:{{ row.target_name }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="table_name" label="表" width="140" />
        <el-table-column label="行级过滤(row_filter)" min-width="220">
          <template #default="{ row }">
            <code class="sql-expr">{{ row.row_filter || '-' }}</code>
          </template>
        </el-table-column>
        <el-table-column label="列级访问(col_access)" min-width="280">
          <template #default="{ row }">
            <el-tag v-for="(mode, col) in row.col_access" :key="col" size="small" class="col-tag"
              :type="mode === 'allow' ? 'success' : mode === 'mask' ? 'warning' : 'danger'">
              {{ col }}:{{ modeText[mode] || mode }}
            </el-tag>
            <span v-if="!Object.keys(row.col_access || {}).length" class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="掩码表达式" min-width="140">
          <template #default="{ row }"><code class="sql-expr">{{ row.mask_expression || "'***'" }}</code></template>
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

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑规则' : '新建规则'" width="680px" destroy-on-close>
      <el-form :model="form" label-width="120px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="目标类型" required>
              <el-radio-group v-model="form.target_type">
                <el-radio value="role">角色</el-radio>
                <el-radio value="user">用户</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="目标名称" required>
              <el-input v-model="form.target_name" placeholder="role: user/approver/admin; user: 用户名" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="业务表" required>
          <el-input v-model="form.table_name" placeholder="如 orders / customers" style="width: 320px" />
          <el-button style="margin-left: 8px" :loading="loadingCols" @click="loadColumns">加载列</el-button>
        </el-form-item>
        <el-form-item label="行级过滤">
          <el-input v-model="form.row_filter" type="textarea" :rows="2"
            placeholder="如 order_date >= now() - interval '90 days'(只看最近90天订单)" />
        </el-form-item>
        <el-form-item label="列级访问">
          <div class="col-editor">
            <div v-for="(item, i) in colRows" :key="i" class="col-editor-row">
              <el-select v-model="item.col" filterable allow-create default-first-option style="width: 220px"
                placeholder="选择列(可输入)" :disabled="!columnNames.length && !item.col">
                <el-option v-for="c in columnNames" :key="c" :label="columnLabel(c)" :value="c" />
              </el-select>
              <el-select v-model="item.mode" style="width: 130px; margin-left: 8px">
                <el-option label="可见(原样显示)" value="allow" />
                <el-option label="脱敏(打码显示)" value="mask" />
                <el-option label="禁止(查不到)" value="deny" />
              </el-select>
              <el-button style="margin-left: 8px" text type="danger" :icon="Delete" @click="colRows.splice(i, 1)" />
            </div>
            <div class="col-actions">
              <el-button size="small" :icon="Plus" @click="colRows.push({ col: '', mode: 'mask' })">添加列</el-button>
              <span class="tip">未列出的列默认"可见"。</span>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="脱敏方式">
          <el-select v-model="form.maskTemplate" style="width: 100%">
            <el-option label="完全打码(***)" value="'***'" />
            <el-option label="手机号部分隐藏(如 138****5678)" value="substr(phone,1,3)||'****'||substr(phone,8,4)" />
            <el-option label="身份证部分隐藏(如 1101**********1234)" value="substr(id_card,1,4)||'**********'||substr(id_card,15,4)" />
          </el-select>
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
import { Plus, Refresh, Delete } from '@element-plus/icons-vue'
import { createDataPolicy, deleteDataPolicy, fetchSchemaTables, listDataPolicies, updateDataPolicy } from '@/api'

const rows = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)

const emptyForm = () => ({
  id: null,
  target_type: 'role',
  target_name: 'user',
  table_name: '',
  row_filter: '',
  maskTemplate: "'***'",
  enabled: true
})
const form = reactive(emptyForm())
const colRows = ref([])
const columnNames = ref([])
const columnsMeta = ref([]) // 完整列对象(含中文名 comment)
const loadingCols = ref(false)
const modeText = { allow: '可见', mask: '脱敏', deny: '禁止' }

async function loadColumns() {
  if (!form.table_name.trim()) {
    ElMessage.warning('请先填写业务表名')
    return
  }
  loadingCols.value = true
  try {
    const data = await fetchSchemaTables({ data_source_id: null }) // 主库
    const t = (data.tables || []).find((x) => x.name === form.table_name.trim())
    if (!t) {
      ElMessage.warning(`主库中未找到表「${form.table_name.trim()}」,可手动输入列名`)
      columnNames.value = []
      return
    }
    columnNames.value = t.columns.map((c) => c.name)
    columnsMeta.value = t.columns
    ElMessage.success(`已加载 ${columnNames.value.length} 个字段`)
  } catch (e) {
    columnNames.value = []
    columnsMeta.value = []
  } finally {
    loadingCols.value = false
  }
}

function columnLabel(colName) {
  const c = columnsMeta.value.find((x) => x.name === colName)
  return c && c.comment ? `${c.name}（${c.comment}）` : colName
}

function colAccessFromRows() {
  const out = {}
  for (const item of colRows.value) {
    const col = (item.col || '').trim()
    if (col) out[col] = item.mode
  }
  return out
}

async function load() {
  loading.value = true
  try {
    const data = await listDataPolicies()
    rows.value = data.policies || []
  } catch (e) {
    /* 已提示 */
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  colRows.value = []
  columnNames.value = []
  dialogVisible.value = true
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id,
    target_type: row.target_type,
    target_name: row.target_name,
    table_name: row.table_name,
    row_filter: row.row_filter || '',
    maskTemplate: row.mask_expression || "'***'",
    enabled: !!row.enabled
  })
  colRows.value = Object.entries(row.col_access || {}).map(([col, mode]) => ({ col, mode }))
  columnNames.value = []
  loadColumns() // 尝试自动加载该表列, 供下拉选择
  dialogVisible.value = true
}

async function save() {
  if (!form.target_name.trim() || !form.table_name.trim()) {
    ElMessage.warning('目标名称与业务表必填')
    return
  }
  const payload = {
    target_type: form.target_type,
    target_name: form.target_name.trim(),
    table_name: form.table_name.trim(),
    row_filter: form.row_filter.trim(),
    col_access: colAccessFromRows(),
    mask_expression: form.maskTemplate,
    enabled: form.enabled
  }
  saving.value = true
  try {
    if (form.id) {
      await updateDataPolicy(form.id, payload)
      ElMessage.success('规则已更新')
    } else {
      await createDataPolicy(payload)
      ElMessage.success('规则已创建')
    }
    dialogVisible.value = false
    load()
  } catch (e) {
    /* 已提示 */
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(row, val) {
  try {
    await updateDataPolicy(row.id, { enabled: val })
    row.enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
  } catch (e) {
    /* 已提示 */
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除后 ${row.target_type === 'role' ? '角色' : '用户'} ${row.target_name} 对表 ${row.table_name} 恢复默认允许。确认删除?`,
      '删除规则',
      { type: 'warning' }
    )
  } catch {
    return
  }
  await deleteDataPolicy(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.page-header p {
  color: #888;
  font-size: 13px;
  margin-top: 4px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}
.tip {
  color: #999;
  font-size: 12px;
  margin-left: 8px;
}
.muted {
  color: #999;
}
.sql-expr {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #555;
}
.col-tag {
  margin: 2px 4px 2px 0;
}
.col-editor-row {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
}
.col-editor .tip {
  margin-left: 0;
  display: block;
  margin-top: 6px;
}
</style>
