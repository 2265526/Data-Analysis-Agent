<template>
  <div>
    <div class="page-header">
      <h2>📐 指标口径</h2>
      <p>定义平台的"标准算法",比如"销售额"怎么算。定义后,所有人问"销售额"都是同一个算法。</p>
    </div>

    <div class="panel-card">
      <div class="toolbar">
        <el-button type="primary" :icon="Plus" @click="openCreate">新增指标</el-button>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="name_cn" label="指标名称" width="140" />
        <el-table-column prop="name_en" label="标识" width="140" />
        <el-table-column label="算法" min-width="170">
          <template #default="{ row }">
            <el-tag size="small">{{ aggText(row.agg) }}</el-tag>
            <code class="sql-expr"> {{ row.expr }}</code>
            <div v-if="row.filter" class="muted small">范围: {{ row.filter }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="unit" label="单位" width="70" />
        <el-table-column prop="category" label="分类" width="90" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status === 'active' ? '启用' : '已下线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="row.status === 'active'" size="small" text type="danger" @click="onDeprecate(row)">下线</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑指标' : '新增指标'" width="660px" destroy-on-close>
      <el-form :model="form" label-width="110px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="指标名称" required>
              <el-input v-model="form.name_cn" placeholder="如 近7天销售额" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="英文标识" required>
              <el-input v-model="form.name_en" :disabled="!!form.id" placeholder="sales_7d(创建后不可改)" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="别名">
          <el-input v-model="form.aliasText" placeholder="逗号分隔, 如 销售额,GMV,成交额" />
        </el-form-item>

        <el-divider content-position="left">口径定义</el-divider>

        <el-form-item v-if="dataSources.length" label="数据来源">
          <el-select v-model="form.dsForPickers" multiple filterable collapse-tags style="width: 100%"
            placeholder="演示数据(不选) · 可多选跨库" @change="loadTables">
            <el-option v-for="ds in dataSources" :key="ds.id" :label="ds.name" :value="ds.id" />
          </el-select>
          <div class="tip">可多选多个数据源,表清单会合并展示;不选=演示数据。</div>
        </el-form-item>
        <el-form-item label="数据表">
          <el-select v-model="form.pickedTable" filterable allow-create default-first-option style="width: 100%"
            placeholder="选择表, 或直接输入表名" @change="onPickTable">
            <el-option v-for="t in tableOptions" :key="t.name" :label="tableLabel(t)" :value="t.name" />
          </el-select>
          <div v-if="!tableOptions.length" class="tip">未读取到表清单,可直接输入表名。</div>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="14">
            <el-form-item label="对应字段">
              <el-select v-model="form.pickedColumn" filterable allow-create default-first-option style="width: 100%"
                placeholder="选择列, 或直接输入列名" @change="onPickColumn">
                <el-option v-for="c in columnOptions" :key="c.name" :label="columnLabel(c)" :value="c.name" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="算法">
              <el-select v-model="form.agg" style="width: 100%">
                <el-option v-for="(label, v) in aggOptions" :key="v" :label="label" :value="v" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item v-if="form.agg === 'custom'" label="自定义算法" required>
          <el-input v-model="form.customAgg" type="textarea" :rows="2"
            placeholder="填写完整的聚合表达式, 如 sum(case when o.order_status='已完成' then 1 else 0 end)" />
        </el-form-item>

        <el-form-item label="统计范围">
          <el-select v-model="form.filterType" style="width: 220px" @change="onFilterTypeChange">
            <el-option label="全部数据" value="all" />
            <el-option label="时间范围" value="time" />
            <el-option label="自定义条件" value="custom" />
          </el-select>
        </el-form-item>
        <template v-if="form.filterType === 'time'">
          <el-form-item label="日期字段">
            <el-select v-model="form.dateColumn" style="width: 100%" placeholder="选择日期字段"
              :disabled="!dateColumns.length">
              <el-option v-for="c in dateColumns" :key="c.name" :label="c.name" :value="c.name" />
            </el-select>
            <div v-if="!dateColumns.length" class="tip">未识别到日期字段,可直接输入列名。</div>
          </el-form-item>
          <el-form-item label="统计多久">
            <el-select v-model="form.timeSpan" style="width: 100%">
              <el-option label="最近 7 天" value="last_7d" />
              <el-option label="最近 30 天" value="last_30d" />
              <el-option label="最近 90 天" value="last_90d" />
              <el-option label="最近 180 天" value="last_180d" />
              <el-option label="最近 1 年" value="last_1y" />
              <el-option label="本月" value="this_month" />
              <el-option label="本季度" value="this_quarter" />
              <el-option label="今年" value="this_year" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item v-if="form.filterType === 'custom'" label="自定义条件">
          <el-input v-model="form.customFilter" type="textarea" :rows="2"
            placeholder="如 o.order_status = '已完成', 或 省 = '广东'" />
        </el-form-item>
        <el-form-item label="口径预览">
          <div class="preview">
            <div>
              算法: <el-tag size="small">{{ aggText(form.agg) }}</el-tag>
              <span class="muted small">
                {{ form.agg === 'custom' ? form.customAgg || '(待填写)' : (form.expr || '(待选择)') }}
              </span>
            </div>
            <div v-if="buildFilter()" class="muted small">统计范围: {{ buildFilter() }}</div>
          </div>
        </el-form-item>

        <el-form-item label="单位">
          <el-input v-model="form.unit" placeholder="元 / 笔 / %" style="width: 200px" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="这个指标怎么算,便于后续维护" />
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
import { Plus, Refresh } from '@element-plus/icons-vue'
import { createMetricDefinition, deleteMetricDefinition, listDataSources, listMetricDefinitions, fetchSchemaTables, updateMetricDefinition } from '@/api'

const rows = ref([])
const dataSources = ref([])
const tables = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)

const aggOptions = {
  sum: '求和(加起来)', count: '计数(条数)', count_distinct: '去重计数(不重复的条数)',
  avg: '平均(平均值)', max: '最大(最大值)', min: '最小(最小值)', custom: '自定义(高级)'
}
const aggText = (v) => aggOptions[v] || v || '-'

const emptyForm = () => ({
  id: null, name_cn: '', name_en: '', aliasText: '', agg: 'sum',
  customAgg: '', expr: '', filterType: 'all', dateColumn: '', timeSpan: 'last_7d',
  customFilter: '', unit: '', description: '',
  dsForPickers: [], pickedTable: '', pickedColumn: ''
})
const form = reactive(emptyForm())

const tableOptions = computed(() => tables.value)
const columnOptions = computed(() => {
  const t = tables.value.find((x) => x.name === form.pickedTable)
  return t ? t.columns : []
})
// 下拉显示: 英文名(中文名) —— 业务用户看得懂
function tableLabel(t) {
  return t.comment ? `${t.name}（${t.comment}）` : t.name
}
function columnLabel(c) {
  const typePart = c.data_type ? `(${c.data_type})` : ''
  return c.comment ? `${c.name}${typePart}（${c.comment}）` : `${c.name}${typePart}`
}
// 日期类型列(统计范围"时间范围"用)
const dateColumns = computed(() =>
  columnOptions.value.filter((c) => /date|time|timestamp/i.test(c.data_type || ''))
)

// 时间跨度 -> SQL 条件(用裸列名, 避免别名问题; 多表歧义时用户可在自定义条件里写限定)
const TIME_SPAN_SQL = {
  last_7d: " >= now() - interval '7 days'",
  last_30d: " >= now() - interval '30 days'",
  last_90d: " >= now() - interval '90 days'",
  last_180d: " >= now() - interval '180 days'",
  last_1y: " >= now() - interval '1 year'",
  this_month: " >= date_trunc('month', now())",
  this_quarter: " >= date_trunc('quarter', now())",
  this_year: " >= date_trunc('year', now())"
}

function buildFilter() {
  if (form.filterType === 'time' && form.dateColumn && form.timeSpan) {
    return form.dateColumn + TIME_SPAN_SQL[form.timeSpan]
  }
  if (form.filterType === 'custom') return form.customFilter.trim()
  return ''
}

function onFilterTypeChange() {
  // 切到"时间范围"时自动带出日期字段(若有)
  if (form.filterType === 'time' && !form.dateColumn && dateColumns.value.length) {
    form.dateColumn = dateColumns.value[0].name
  }
}

async function load() {
  loading.value = true
  try {
    const [m, ds] = await Promise.all([listMetricDefinitions(), listDataSources()])
    rows.value = m.metrics || []
    dataSources.value = ds.sources || []
  } catch (e) { /* 已提示 */ } finally {
    loading.value = false
  }
}

async function loadTables() {
  tables.value = []
  form.pickedTable = ''
  form.pickedColumn = ''
  form.expr = ''
  try {
    // 未选数据源=演示数据(主库); 多选=并行拉取各库表清单并合并(同名表合并, 中文名取首个非空)
    const ids = form.dsForPickers.length ? form.dsForPickers : [null]
    const results = await Promise.all(
      ids.map((id) => fetchSchemaTables({ data_source_id: id }).catch(() => ({ tables: [] })))
    )
    const merged = new Map()
    for (const r of results) {
      for (const t of r.tables || []) {
        if (!merged.has(t.name)) {
          merged.set(t.name, { name: t.name, comment: t.comment || '', columns: t.columns || [] })
        } else {
          const cur = merged.get(t.name)
          if (!cur.comment && t.comment) cur.comment = t.comment
          if (!cur.columns.length) cur.columns = t.columns || []
        }
      }
    }
    tables.value = [...merged.values()]
  } catch (e) { tables.value = [] }
}

function onPickTable() {
  form.pickedColumn = ''
  form.expr = ''
}

function onPickColumn() {
  // 自动生成口径表达式: 表名.列名(LLM 可直接使用); 已选列时覆盖
  if (form.pickedTable && form.pickedColumn) {
    form.expr = `${form.pickedTable}.${form.pickedColumn}`
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  loadTables() // 自动加载表清单, "数据表"直接可选
  dialogVisible.value = true
}

function openEdit(row) {
  const f = row.filter || ''
  // 把已有 filter 拆回结构化表单: 时间范围 / 自定义 / 全部
  let filterType = 'all'
  let dateColumn = ''
  let timeSpan = 'last_7d'
  let customFilter = ''
  const t = tables.value.find((x) => x.name === form.pickedTable)
  const dateCols = t ? t.columns.filter((c) => /date|time|timestamp/i.test(c.data_type || '')).map((c) => c.name) : []
  if (f) {
    const m = f.match(/^(\w+)\s*>= (now\(\) - interval '(\d+) (days|months|years)'|date_trunc\('(month|quarter|year)', now\(\)\))/)
    if (m) {
      filterType = 'time'
      dateColumn = m[1]
      if (m[3]) {
        const unit = m[4]
        const spanMap = { days: { 7: 'last_7d', 30: 'last_30d', 90: 'last_90d', 180: 'last_180d' }, years: { 1: 'last_1y' } }
        timeSpan = spanMap[unit]?.[Number(m[3])] || (unit === 'years' ? 'last_1y' : 'last_30d')
      } else {
        timeSpan = `this_${m[5]}`
      }
      if (dateCols.length && !dateCols.includes(dateColumn)) dateColumn = ''
    } else {
      filterType = 'custom'
      customFilter = f
    }
  }
  Object.assign(form, emptyForm(), {
    id: row.id, name_cn: row.name_cn, name_en: row.name_en,
    aliasText: (row.alias || []).join(', '),
    agg: row.agg,
    customAgg: row.agg === 'custom' ? (row.expr || '') : '',
    expr: row.expr || '',
    filterType, dateColumn, timeSpan, customFilter,
    unit: row.unit || '', description: row.description || ''
  })
  loadTables() // 自动加载表清单
  dialogVisible.value = true
}

async function save() {
  if (!form.name_cn.trim() || !form.name_en.trim()) {
    ElMessage.warning('指标名称与英文标识必填')
    return
  }
  let agg = form.agg
  let expr = form.expr.trim()
  if (agg === 'custom') {
    if (!form.customAgg.trim()) {
      ElMessage.warning('请填写自定义算法表达式')
      return
    }
    expr = form.customAgg.trim()
  } else if (!expr) {
    ElMessage.warning('请选择"对应字段"(或用自定义算法)')
    return
  }
  const filter = buildFilter()
  if (form.filterType === 'time' && !form.dateColumn) {
    ElMessage.warning('请选择日期字段')
    return
  }
  if (form.filterType === 'custom' && !form.customFilter.trim()) {
    ElMessage.warning('请填写自定义条件')
    return
  }
  const payload = {
    name_en: form.name_en.trim(),
    name_cn: form.name_cn.trim(),
    alias: form.aliasText.split(',').map((s) => s.trim()).filter(Boolean),
    agg,
    expr,
    filter,
    unit: form.unit.trim(),
    source_tables: form.pickedTable ? [form.pickedTable] : [],
    category: 'general',
    description: form.description.trim()
  }
  saving.value = true
  try {
    if (form.id) {
      await updateMetricDefinition(form.id, payload)
      ElMessage.success('已更新(对分析立即生效)')
    } else {
      await createMetricDefinition(payload)
      ElMessage.success('已创建(对分析立即生效)')
    }
    dialogVisible.value = false
    load()
  } catch (e) { /* 已提示 */ } finally {
    saving.value = false
  }
}

async function onDeprecate(row) {
  try {
    await ElMessageBox.confirm(`确认下线指标「${row.name_cn}」?之后的提问将不再使用该口径,历史定义保留。`, '下线指标', { type: 'warning' })
  } catch { return }
  await deleteMetricDefinition(row.id)
  ElMessage.success('已下线')
  load()
}

onMounted(load)
</script>

<style scoped>
.page-header p { color: #888; font-size: 13px; margin-top: 4px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.tip { color: #999; font-size: 12px; margin-top: 4px; }
.muted { color: #999; }
.small { font-size: 12px; }
.sql-expr { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 12px; color: #555; }
.preview { width: 100%; background: #f9fafb; border-radius: 8px; padding: 10px 12px; font-size: 13px; line-height: 1.8; }
</style>
