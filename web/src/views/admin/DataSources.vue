<template>
  <div>
    <div class="page-header">
      <h2>🗄️ 数据源</h2>
      <p>接入你的业务数据库(PostgreSQL),接入后即可在数据分析里直接提问。连接信息加密保存,支持测试连接并选择要分析的表格。</p>
    </div>

    <div class="panel-card">
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">接入数据源</el-button>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
        <span class="tip">连接信息仅平台可见(AES-256-GCM 加密存储)。</span>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="数据库地址" min-width="180">
          <template #default="{ row }">
            <span v-if="row.conn_fields">{{ row.conn_fields.host }}:{{ row.conn_fields.port }}/{{ row.conn_fields.dbname }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="允许分析的表" min-width="200">
          <template #default="{ row }">
            <el-tag v-for="t in row.tables_whitelist" :key="t" size="small" class="col-tag">{{ t }}</el-tag>
            <span v-if="!row.tables_whitelist.length" class="muted">全部表</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="150">
          <template #default="{ row }">{{ row.description || '-' }}</template>
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

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑数据源' : '接入数据源'" width="680px" destroy-on-close>
      <el-form :model="form" label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如 业务主库 / 供应链分析库" />
        </el-form-item>

        <el-form-item label="快捷填充">
          <el-select v-model="form.template" style="width: 100%" @change="applyTemplate">
            <el-option label="手动填写(默认)" value="" />
            <el-option label="本机演示库(供应链数据)" value="demo" />
          </el-select>
          <div class="tip">选"本机演示库"会自动填好下方地址等信息,也可直接手动填写。</div>
        </el-form-item>

        <el-row :gutter="12">
          <el-col :span="14">
            <el-form-item label="数据库地址" required>
              <el-input v-model="form.host" placeholder="如 db.example.com 或 127.0.0.1" />
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="端口">
              <el-input v-model.number="form.port" placeholder="5432" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="数据库名" required>
          <el-input v-model="form.dbname" placeholder="要分析的数据库名" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="用户名">
              <el-input v-model="form.user" placeholder="只读账号更安全" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" show-password
                :placeholder="form.id ? '不修改请留空' : ''" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="测试连接">
          <el-button :loading="testing" :icon="Connection" @click="onTest">测试连接并读取表</el-button>
          <span v-if="testResult" class="test-ok">{{ testResult }}</span>
        </el-form-item>

        <el-form-item v-if="tables.length" label="允许分析的表">
          <div class="table-picker">
            <div class="picker-actions">
              <el-checkbox :model-value="allChecked" @change="toggleAll">全选 / 全不选</el-checkbox>
              <span class="tip">不勾选任何表 = 全部表都可分析</span>
            </div>
            <el-checkbox-group v-model="form.tables_whitelist" class="picker-grid">
              <el-checkbox v-for="t in tables" :key="t.name" :value="t.name" :label="tableLabel(t)" />
            </el-checkbox-group>
          </div>
        </el-form-item>

        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="可选,如「这个库是销售数据」" />
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
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Connection } from '@element-plus/icons-vue'
import { createDataSource, deleteDataSource, listDataSources, fetchSchemaTables, updateDataSource } from '@/api'

const rows = ref([])
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const dialogVisible = ref(false)
const tables = ref([])
const testResult = ref('')

const emptyForm = () => ({
  id: null, name: '', template: '', host: '', port: 5432, dbname: '',
  user: '', password: '', tables_whitelist: [], description: '', enabled: true
})
const form = reactive(emptyForm())

const allChecked = computed(() => tables.value.length > 0 && form.tables_whitelist.length === tables.value.length)

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}

async function load() {
  loading.value = true
  try {
    const data = await listDataSources()
    rows.value = data.sources || []
  } catch (e) { /* 已提示 */ } finally {
    loading.value = false
  }
}

function applyTemplate() {
  if (form.template === 'demo') {
    form.host = 'localhost'
    form.port = 5433
    form.dbname = 'data_agent'
    form.user = 'postgres'
  } else {
    // 手动填写: 清空模板字段, 方便直接输入
    form.host = ''
    form.port = 5432
    form.dbname = ''
    form.user = ''
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  tables.value = []
  testResult.value = ''
  dialogVisible.value = true
}

function openEdit(row) {
  Object.assign(form, emptyForm(), {
    id: row.id,
    name: row.name,
    host: row.conn_fields?.host || '',
    port: row.conn_fields?.port || 5432,
    dbname: row.conn_fields?.dbname || '',
    user: row.conn_fields?.user || '',
    password: '', // 密码不回显, 留空=不修改
    tables_whitelist: row.tables_whitelist || [],
    description: row.description || '',
    enabled: !!row.enabled
  })
  tables.value = (row.tables_whitelist || []).map((n) => ({ name: n, columns: [] }))
  testResult.value = ''
  dialogVisible.value = true
}

function connPayload() {
  return {
    host: form.host.trim(),
    port: form.port || 5432,
    dbname: form.dbname.trim(),
    user: form.user.trim(),
    password: form.password
  }
}

function tableLabel(t) {
  return t.comment ? `${t.name}（${t.comment}）` : t.name
}

async function onTest() {
  if (!form.host.trim() || !form.dbname.trim()) {
    ElMessage.warning('请先填写数据库地址和数据库名')
    return
  }
  testing.value = true
  testResult.value = ''
  try {
    const data = await fetchSchemaTables({ conn_fields: connPayload() })
    tables.value = data.tables || []
    testResult.value = `连接成功,共 ${data.total} 张表`
  } catch (e) {
    tables.value = []
  } finally {
    testing.value = false
  }
}

function toggleAll(val) {
  form.tables_whitelist = val ? tables.value.map((t) => t.name) : []
}

async function save() {
  if (!form.name.trim() || !form.host.trim() || !form.dbname.trim()) {
    ElMessage.warning('名称、数据库地址、数据库名必填')
    return
  }
  const payload = {
    name: form.name.trim(),
    conn_fields: connPayload(),
    tables_whitelist: form.tables_whitelist,
    description: form.description.trim(),
    enabled: form.enabled
  }
  saving.value = true
  try {
    if (form.id) {
      await updateDataSource(form.id, payload)
      ElMessage.success('已更新')
    } else {
      await createDataSource(payload)
      ElMessage.success('已接入,现在可以在数据分析里提问了')
    }
    dialogVisible.value = false
    load()
  } catch (e) { /* 已提示 */ } finally {
    saving.value = false
  }
}

async function toggleEnabled(row, val) {
  try {
    await updateDataSource(row.id, { enabled: val })
    row.enabled = val
    ElMessage.success(val ? '已启用' : '已停用')
  } catch (e) { /* 已提示 */ }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除数据源「${row.name}」?历史任务会回退到主库执行。`, '删除数据源', { type: 'warning' })
  } catch { return }
  await deleteDataSource(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.page-header p { color: #888; font-size: 13px; margin-top: 4px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.tip { color: #999; font-size: 12px; margin-top: 4px; line-height: 1.6; }
.muted { color: #999; }
.col-tag { margin: 2px 4px 2px 0; }
.test-ok { color: #16a34a; font-size: 13px; margin-left: 10px; }
.table-picker { width: 100%; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; }
.picker-actions { margin-bottom: 8px; display: flex; align-items: center; gap: 12px; }
.picker-grid { display: flex; flex-wrap: wrap; gap: 4px 18px; }
</style>
