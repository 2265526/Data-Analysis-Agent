import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true, title: '登录' }
  },
  {
    path: '/',
    component: () => import('@/layouts/AdminLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '工作台', icon: 'HomeFilled' }
      },
      {
        path: 'analysis',
        name: 'Analysis',
        component: () => import('@/views/analysis/Index.vue'),
        meta: { title: '数据分析', icon: 'DataAnalysis' }
      },
      {
        path: 'board/:taskId',
        name: 'Board',
        component: () => import('@/views/analysis/Board.vue'),
        meta: { title: '交互式看板' }
      },
      {
        path: 'tasks',
        name: 'Tasks',
        component: () => import('@/views/Tasks.vue'),
        meta: { title: '任务历史', icon: 'Tickets' }
      },
      {
        path: 'profile',
        name: 'Profile',
        component: () => import('@/views/Profile.vue'),
        meta: { title: '个人中心', icon: 'User' }
      },
      {
        path: 'admin/users',
        name: 'AdminUsers',
        component: () => import('@/views/admin/Users.vue'),
        meta: { title: '用户管理', icon: 'User', admin: true }
      },
      {
        path: 'admin/data-policies',
        name: 'DataPolicies',
        component: () => import('@/views/admin/DataPolicies.vue'),
        meta: { title: '数据权限', icon: 'Lock', admin: true }
      },
      {
        path: 'admin/data-sources',
        name: 'DataSources',
        component: () => import('@/views/admin/DataSources.vue'),
        meta: { title: '数据源', icon: 'Coin', admin: true }
      },
      {
        path: 'admin/metric-definitions',
        name: 'MetricDefinitions',
        component: () => import('@/views/admin/MetricDefinitions.vue'),
        meta: { title: '指标口径', icon: 'DataAnalysis', admin: true }
      },
      {
        path: 'admin/schema-dict',
        name: 'SchemaDict',
        component: () => import('@/views/admin/SchemaDict.vue'),
        meta: { title: '数据字典', icon: 'Notebook', admin: true }
      },
      {
        path: 'scheduled-tasks',
        name: 'ScheduledTasks',
        component: () => import('@/views/admin/ScheduledTasks.vue'),
        meta: { title: '定时任务', icon: 'Clock' }
      },
      {
        path: 'admin/metrics',
        name: 'AdminMetrics',
        component: () => import('@/views/admin/Metrics.vue'),
        meta: { title: '指标看板', icon: 'Odometer', admin: true }
      },
      {
        path: 'admin/audit-logs',
        name: 'AuditLogs',
        component: () => import('@/views/admin/AuditLogs.vue'),
        meta: { title: '操作日志', icon: 'Document', admin: true }
      },
      {
        path: 'approvals',
        name: 'Approvals',
        component: () => import('@/views/Approvals.vue'),
        meta: { title: '审批中心', icon: 'Stamp', approver: true }
      },
      {
        path: '403',
        name: 'Forbidden',
        component: () => import('@/views/error/Forbidden.vue'),
        meta: { title: '无权限' }
      }
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/error/NotFound.vue'),
    meta: { title: '页面不存在' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach((to) => {
  const store = useUserStore()
  if (!to.meta.public && !store.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.admin && !store.isAdmin) {
    return { path: '/403' }
  }
  if (to.meta.approver && !(store.isAdmin || store.roles.includes('approver'))) {
    return { path: '/403' }
  }
  // 管理员不提供数据分析页(菜单不展示, 直接访问也重定向到用户管理)
  if (store.isAdmin && to.path === '/analysis') {
    return { path: '/admin/users' }
  }
  // 工作台仅管理员可见, 普通用户访问重定向到数据分析
  if (!store.isAdmin && to.path === '/dashboard') {
    return { path: '/analysis' }
  }
  if (to.path === '/login' && store.isLoggedIn) {
    return { path: store.isAdmin ? '/dashboard' : '/analysis' }
  }
  document.title = `${to.meta.title || ''} · Data Pipeline Agent`
})

export default router
