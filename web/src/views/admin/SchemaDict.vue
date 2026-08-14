<template>
  <div>
    <div class="page-header">
      <h2>📖 数据字典</h2>
      <p>给数据库的表和字段补上中文名。补完后,所有下拉选择(定义指标/配权限/选表)都会显示"英文名(中文名)"。</p>
    </div>

    <div class="panel-card">
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">新增</el-button>
        <el-input v-model="keyword" placeholder="搜索 表名/字段名/中文名" clearable style="width: 240px"
          @keyup.enter="load" @clear="load" />
        <el-button :icon="Search" @click="load">搜索</el-button>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="table_name" label="表" width="180">
          <template #default="{ row }">
            {{ row.table_name }}
            <span v-if="tableCn(row.table_name)" class="muted">（{{ tableCn(row.table_name) }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="字段" width="200">
          <template #default="{ row }">
            <el-tag v-if="!row.column_name" size="small" type="info">整张表</el-tag>
            <template v-else>{{ row.column_name }}</template>
          </template>
        </el-table-column>
        <el-table-column prop="cn_name" label="中文名" min-width="180" />
        <el-table-column prop="updated_at" label="更新时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑字典项' : '新增字典项'" width="560px" destroy-on-close>
      <el-form :model="form" label-width="90px">
        <el-form-item label="表" required>
          <el-select v-model="form.table_name" filterable allow-create default-first-option style="width: 100%"
            placeholder="选择或输入表名" @change="onPickTable">
            <el-option v-for="t in schemaTables" :key="t.name" :label="tableLabel(t)" :value="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="字段">
          <el-select v-model="form.column_name" clearable filterable allow-create default-first-option
            style="width: 100%" placeholder="整张表(不选字段)">
            <el-option label="(整张表)" value="" />
            <el-option v-for="c in columnOptions" :key="c.name" :label="columnLabel(c)" :value="c.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="中文名" required>
          <el-input v-model="form.cn_name" placeholder="如 订单表 / 订单号 / 手机号" />
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
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { createSchemaDict, deleteSchemaDict, fetchSchemaTables, listSchemaDict, updateSchemaDict } from '@/api'

const rows = ref([])
const schemaTables = ref([])
const keyword = ref('')
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)

const emptyForm = () => ({ id: null, table_name: '', column_name: '', cn_name: '' })
const form = reactive(emptyForm())

const columnOptions = computed(() => {
  const t = schemaTables.value.find((x) => x.name === form.table_name)
  return t ? t.columns : []
})

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}
function tableLabel(t) {
  return t.comment ? `${t.name}（${t.comment}）` : t.name
}
function columnLabel(c) {
  return c.comment ? `${c.name}（${c.comment}）` : c.name
}
// 表格里展示"表(中文)"——查主库清单里的 comment
function tableCn(name) {
  const t = schemaTables.value.find((x) => x.name === name)
  return t ? t.comment : ''
}

async function load() {
  loading.value = true
  try {
    const params = keyword.value.trim() ? { keyword: keyword.value.trim() } : {}
    const [d, st] = await Promise.all([listSchemaDict(params), fetchSchemaTables({ data_source_id: null })])
    rows.value = d.items || []
    schemaTables.value = st.tables || []
  } catch (e) { /* 已提示 */ } finally {
    loading.value = false
  }
}

function onPickTable() {
  form.column_name = ''
}

function openCreate() {
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id, table_name: row.table_name, column_name: row.column_name, cn_name: row.cn_name
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.table_name.trim() || !form.cn_name.trim()) {
    ElMessage.warning('表名与中文名必填')
    return
  }
  const payload = {
    table_name: form.table_name.trim(),
    column_name: form.column_name || '',
    cn_name: form.cn_name.trim()
  }
  saving.value = true
  try {
    if (form.id) {
      await updateSchemaDict(form.id, payload)
      ElMessage.success('已更新(下拉立即生效)')
    } else {
      await createSchemaDict(payload)
      ElMessage.success('已新增(下拉立即生效)')
    }
    dialogVisible.value = false
    load()
  } catch (e) { /* 已提示 */ } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除「${row.table_name}${row.column_name ? '.' + row.column_name : ''}」的中文名?`,
      '删除字典项', { type: 'warning' }
    )
  } catch { return }
  await deleteSchemaDict(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.page-header p { color: #888; font-size: 13px; margin-top: 4px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.muted { color: #999; font-size: 12px; }
</style>
