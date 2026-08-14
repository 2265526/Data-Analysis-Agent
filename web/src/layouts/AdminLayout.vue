<template>
  <el-container class="layout">
    <!-- 暗色侧边栏 -->
    <el-aside :width="sidebarWidth" class="sidebar">
      <div class="logo">
        <div class="logo-icon">📊</div>
        <div v-show="!collapsed" class="logo-text">
          <div class="t">Data Pipeline Agent</div>
          <div class="s">数据分析智能体平台</div>
        </div>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="transparent"
        text-color="#a3a7bf"
        active-text-color="#ffffff"
        :collapse="collapsed"
        :collapse-transition="false"
        class="side-menu"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <template #title>{{ m.title }}</template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- 顶栏 -->
      <el-header class="header" height="56px">
        <div class="header-left">
          <el-icon class="collapse-btn" @click="collapsed = !collapsed">
            <Expand v-if="collapsed" />
            <Fold v-else />
          </el-icon>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item>{{ currentTitle }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <!-- 站内通知铃铛: 定时任务结果推送 -->
          <el-popover placement="bottom-end" :width="360" trigger="click" @show="loadNotifications">
            <template #reference>
              <el-badge :value="unread" :hidden="!unread" :max="99" class="bell">
                <el-icon class="bell-icon" :size="20"><Bell /></el-icon>
              </el-badge>
            </template>
            <div class="notif-panel">
              <div class="notif-head">
                <b>通知</b>
                <el-button v-if="unread" size="small" text type="primary" @click="readAll">全部已读</el-button>
              </div>
              <el-empty v-if="!notifications.length" description="暂无通知" :image-size="50" />
              <div v-for="n in notifications" :key="n.id" class="notif-item" :class="{ unread: !n.read }" @click="openTask(n)">
                <div class="notif-title">{{ n.title }}</div>
                <div class="notif-content">{{ n.content }}</div>
                <div class="notif-time">{{ fmtTime(n.created_at) }}</div>
              </div>
            </div>
          </el-popover>
          <el-dropdown trigger="click">
            <span class="user-chip">
              <el-avatar :size="28" class="avatar">{{ store.username.charAt(0).toUpperCase() }}</el-avatar>
              <span class="uname">{{ store.username }}</span>
              <el-icon><CaretBottom /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="router.push('/profile')">个人中心</el-dropdown-item>
                <el-dropdown-item disabled>角色: {{ store.roleText }}</el-dropdown-item>
                <el-dropdown-item divided @click="onLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Expand, Fold, CaretBottom, Bell } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { listNotifications, markNotificationsRead } from '@/api'

const store = useUserStore()
const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

// ---- 站内通知 ----
const notifications = ref([])
const unread = ref(0)

async function loadNotifications() {
  try {
    const data = await listNotifications({ page: 1, page_size: 20 })
    notifications.value = data.notifications || []
    unread.value = data.unread || 0
  } catch (e) { /* 已提示 */ }
}

async function readAll() {
  try {
    await markNotificationsRead({ all: true })
    unread.value = 0
    notifications.value = (notifications.value || []).map((n) => ({ ...n, read: true }))
  } catch (e) { /* 已提示 */ }
}

function openTask(n) {
  if (!n.read) {
    markNotificationsRead({ id: n.id }).catch(() => {})
    n.read = true
    if (unread.value > 0) unread.value--
  }
  if (n.task_id) router.push(`/tasks`)
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : ''
}

onMounted(loadNotifications)
const sidebarWidth = computed(() => (collapsed.value ? '64px' : '220px'))

const menus = computed(() => {
  const isApprover = store.isAdmin || store.roles.includes('approver')
  if (store.isAdmin) {
    // 管理员: 工作台 + 用户管理 + 指标看板 + 审批中心 + 操作日志, 无数据分析
    return [
      { path: '/dashboard', title: '工作台', icon: 'HomeFilled' },
      { path: '/tasks', title: '任务历史', icon: 'Tickets' },
      { path: '/admin/users', title: '用户管理', icon: 'User' },
      { path: '/admin/data-policies', title: '数据权限', icon: 'Lock' },
      { path: '/admin/data-sources', title: '数据源', icon: 'Coin' },
      { path: '/admin/metric-definitions', title: '指标口径', icon: 'DataAnalysis' },
      { path: '/admin/schema-dict', title: '数据字典', icon: 'Notebook' },
      { path: '/scheduled-tasks', title: '定时任务', icon: 'Clock' },
      { path: '/admin/metrics', title: '指标看板', icon: 'Odometer' },
      { path: '/admin/audit-logs', title: '操作日志', icon: 'Document' },
      { path: '/approvals', title: '审批中心', icon: 'Stamp' }
    ]
  }
  // 普通用户/审批人: 无工作台(运营指标属管理员), 默认进数据分析
  const base = [
    { path: '/analysis', title: '数据分析', icon: 'DataAnalysis' },
    { path: '/tasks', title: '任务历史', icon: 'Tickets' },
    { path: '/scheduled-tasks', title: '定时任务', icon: 'Clock' }
  ]
  if (isApprover) {
    base.push({ path: '/approvals', title: '审批中心', icon: 'Stamp' })
  }
  return base
})

const activeMenu = computed(() => route.path)
const currentTitle = computed(() => route.meta.title || '')

function onLogout() {
  store.logout()
  router.push('/login')
}
</script>

<style scoped>
.layout { height: 100vh; }
.sidebar {
  background: #1e1f2b;
  transition: width 0.2s ease;
  overflow: hidden;
}
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
}
.logo-icon { font-size: 26px; }
.logo-text .t { color: #fff; font-weight: 800; font-size: 14px; white-space: nowrap; }
.logo-text .s { color: #8b8fa8; font-size: 11px; white-space: nowrap; margin-top: 2px; }
.side-menu { border-right: none; padding: 4px 10px; }
.side-menu :deep(.el-menu-item) { height: 46px; border-radius: 10px; margin-bottom: 4px; }
.side-menu :deep(.el-menu-item.is-active) {
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
}
.side-menu :deep(.el-menu-item:hover) { background: rgba(255, 255, 255, 0.06); }
.header {
  background: #fff;
  border-bottom: 1px solid #eceff5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}
.header-left { display: flex; align-items: center; gap: 14px; }
.header-right {
  display: flex;
  align-items: center;
  gap: 18px;
}
.bell { cursor: pointer; display: flex; align-items: center; color: #4b5563; }
.bell-icon:hover { color: #4f46e5; }
.notif-panel { max-height: 420px; overflow: auto; }
.notif-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.notif-item { padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; }
.notif-item:hover { background: #f3f4f6; }
.notif-item.unread { background: #eef2ff; }
.notif-title { font-size: 13px; font-weight: 600; color: #111827; }
.notif-content { font-size: 12px; color: #6b7280; margin-top: 2px; }
.notif-time { font-size: 11px; color: #9ca3af; margin-top: 4px; }
.collapse-btn { font-size: 18px; cursor: pointer; color: #6b7280; }
.collapse-btn:hover { color: #4f46e5; }
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #111827;
  font-weight: 600;
  font-size: 14px;
}
.avatar {
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  color: #fff;
  font-weight: 700;
}
.main {
  padding: 20px 24px;
  overflow: auto;
}
</style>
