<template>
  <div>
    <div class="page-header">
      <h2>👥 用户管理</h2>
      <p>查看注册用户信息, 重置密码, 或创建新账号(用户本人无自助改密入口)</p>
    </div>

    <!-- 统计卡 -->
    <el-row :gutter="16" style="margin-bottom: 18px">
      <el-col v-for="s in stats" :key="s.lbl" :span="6">
        <div class="stat-card">
          <div class="num">{{ s.num }}</div>
          <div class="lbl">{{ s.lbl }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 用户表格 -->
    <div class="panel-card" style="margin-bottom: 18px">
      <el-table :data="users" stripe style="width: 100%">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="用户名" min-width="140">
          <template #default="{ row }">{{ row.name }}</template>
        </el-table-column>
        <el-table-column label="角色" min-width="140">
          <template #default="{ row }">
            <el-tag :type="roleType(row.roles)" size="small">{{ roleLabel(row.roles) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" min-width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="updated_at" label="更新时间" min-width="160">
          <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="selectUser(row)">详情 / 重置密码</el-button>
            <el-button link type="warning" @click="openRoles(row)">调整权限</el-button>
            <el-button
              link
              type="danger"
              :disabled="isSelf(row)"
              @click="onDelete(row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-row :gutter="16">
      <!-- 用户详情 + 重置密码 -->
      <el-col :span="12">
        <div class="panel-card">
          <h3 style="margin-top: 0">🔑 用户详情与密码重置</h3>
          <el-form v-if="selected" label-width="90px">
            <el-form-item label="用户名">
              <el-input :model-value="selected.name" disabled />
            </el-form-item>
            <el-form-item label="用户 ID">
              <el-input :model-value="selected.id" disabled />
            </el-form-item>
            <el-form-item label="角色">
              <div>
                <el-tag :type="roleType(selected.roles)" size="small">{{ roleLabel(selected.roles) }}</el-tag>
              </div>
            </el-form-item>
            <el-form-item label="密码哈希">
              <el-tooltip :content="selected.password_hash || '-'" placement="top">
                <code class="hash">{{ shortHash(selected.password_hash) }}</code>
              </el-tooltip>
              <div class="hash-tip">PBKDF2 加密存储, 无法还原明文; 遗忘密码请直接重置</div>
            </el-form-item>
            <el-form-item label="创建时间">{{ fmtTime(selected.created_at) }}</el-form-item>
            <el-form-item label="更新时间">{{ fmtTime(selected.updated_at) }}</el-form-item>
            <el-divider />
            <el-form-item label="新密码">
              <el-input v-model="newPwd" type="password" show-password placeholder="至少 6 位" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="resetting" @click="onResetPwd">重置密码</el-button>
            </el-form-item>
          </el-form>
          <el-empty v-else description="从表格选择用户" :image-size="60" />
        </div>
      </el-col>

      <!-- 添加用户 -->
      <el-col :span="12">
        <div class="panel-card">
          <h3 style="margin-top: 0">➕ 添加用户</h3>
          <el-form label-width="90px">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="字母/数字/下划线" />
            </el-form-item>
            <el-form-item label="初始密码">
              <el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" />
            </el-form-item>
            <el-form-item label="角色">
              <el-radio-group v-model="form.role">
                <el-radio value="user">普通用户</el-radio>
                <el-radio value="approver">审批人</el-radio>
                <el-radio value="admin">管理员</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="creating" @click="onCreate">创建用户</el-button>
            </el-form-item>
          </el-form>
        </div>
      </el-col>
    </el-row>

    <!-- 调整权限对话框(单选一种权限) -->
    <el-dialog v-model="rolesDialog" :title="`调整权限 — ${rolesTarget?.name || ''}`" width="420px">
      <el-radio-group v-model="rolesChecked" style="display: flex; flex-direction: column; gap: 10px">
        <el-radio value="user">普通用户(user) — 提交分析任务</el-radio>
        <el-radio value="approver">审批人(approver) — 审批/拒绝任务</el-radio>
        <el-radio value="admin">管理员(admin) — 全部权限</el-radio>
      </el-radio-group>
      <div class="roles-tip">只能选择一种权限; 至少保留一个管理员; 不能修改自己的权限</div>
      <template #footer>
        <el-button @click="rolesDialog = false">取消</el-button>
        <el-button type="primary" :loading="rolesSaving" @click="saveRoles">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listUsers, createUser, resetUserPassword, deleteUser, updateUserRoles } from '@/api'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
const users = ref([])
const selected = ref(null)
const newPwd = ref('')
const resetting = ref(false)
const creating = ref(false)
const form = reactive({ username: '', password: '', role: 'user' })

// 调整权限对话框
const rolesDialog = ref(false)
const rolesTarget = ref(null)
const rolesChecked = ref([])
const rolesSaving = ref(false)

// 单角色展示: 优先级 admin > approver > user(历史多角色数据只取主角色)
const ROLE_META = {
  admin: { label: '管理员', type: 'danger' },
  approver: { label: '审批人', type: 'warning' },
  user: { label: '普通用户', type: 'primary' }
}
function mainRole(roles) {
  const order = ['admin', 'approver', 'user']
  for (const r of order) if (roles?.includes(r)) return r
  return roles?.[0] || 'user'
}
function roleLabel(roles) {
  return ROLE_META[mainRole(roles)]?.label || mainRole(roles)
}
function roleType(roles) {
  return ROLE_META[mainRole(roles)]?.type || 'info'
}

const stats = computed(() => {
  const n = users.value.length
  const admin = users.value.filter((u) => u.roles.includes('admin')).length
  const approver = users.value.filter((u) => u.roles.includes('approver')).length
  return [
    { num: n, lbl: '注册用户' },
    { num: admin, lbl: '管理员' },
    { num: approver, lbl: '审批人' },
    { num: n - admin, lbl: '普通用户' }
  ]
})

function fmtTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

function shortHash(hash) {
  if (!hash) return '-'
  return hash.length > 28 ? hash.slice(0, 28) + '…' : hash
}

function selectUser(row) {
  selected.value = row
  newPwd.value = ''
}

async function load() {
  try {
    const data = await listUsers()
    users.value = data.users || []
  } catch (e) {
    /* 已提示 */
  }
}

async function onResetPwd() {
  if (!selected.value) return
  if (!newPwd.value || newPwd.value.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  resetting.value = true
  try {
    await resetUserPassword(selected.value.id, newPwd.value)
    ElMessage.success(`已重置 ${selected.value.name} 的密码`)
    newPwd.value = ''
  } catch (e) {
    /* 已提示 */
  } finally {
    resetting.value = false
  }
}

async function onCreate() {
  if (!form.username.trim() || !form.password) {
    ElMessage.warning('用户名和密码不能为空')
    return
  }
  creating.value = true
  try {
    await createUser({
      username: form.username.trim(),
      password: form.password,
      roles: [form.role] || ['user']
    })
    ElMessage.success(`用户 ${form.username.trim()} 创建成功`)
    form.username = ''
    form.password = ''
    form.role = 'user'
    load()
  } catch (e) {
    /* 已提示 */
  } finally {
    creating.value = false
  }
}

onMounted(load)

// 当前登录用户(禁止对自己删除/调权)
function isSelf(row) {
  return row.name === store.username
}

function openRoles(row) {
  rolesTarget.value = row
  // 单选: 取主角色(admin > approver > user), 与列表展示的主角色一致
  const order = ['admin', 'approver', 'user']
  const main = order.find((r) => (row.roles || []).includes(r)) || row.roles?.[0] || 'user'
  rolesChecked.value = [main]
  rolesDialog.value = true
}

async function saveRoles() {
  if (!rolesTarget.value) return
  if (!rolesChecked.value.length) {
    ElMessage.warning('至少选择一个角色')
    return
  }
  rolesSaving.value = true
  try {
    await updateUserRoles(rolesTarget.value.id, rolesChecked.value)
    ElMessage.success(`已更新 ${rolesTarget.value.name} 的权限`)
    rolesDialog.value = false
    load()
  } catch (e) {
    /* 已提示 */
  } finally {
    rolesSaving.value = false
  }
}

async function onDelete(row) {
  if (isSelf(row)) return
  try {
    await ElMessageBox.confirm(
      `确定删除用户「${row.name}」吗?\n删除后该用户将无法登录, 其历史任务与审计记录会保留。`,
      '删除用户',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch (e) {
    return // 用户取消
  }
  try {
    await deleteUser(row.id)
    ElMessage.success(`用户 ${row.name} 已删除`)
    if (selected.value?.id === row.id) selected.value = null
    load()
  } catch (e) {
    /* 已提示(后端会拦截: 删除自己/唯一管理员) */
  }
}
</script>

<style scoped>
.hash {
  background: #f3f4f6;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 12px;
}
.hash-tip {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 4px;
}
.roles-tip {
  font-size: 12px;
  color: #9ca3af;
  margin-top: 10px;
}
</style>
