import request from './request'

// ---------- 认证 ----------
export const login = (username, password) =>
  request.post('/auth/login', { username, password })

// ---------- 任务 ----------
export const submitTask = (query, sessionId) =>
  request.post('/tasks', { query, session_id: sessionId ?? null })
export const fetchTaskStatus = (taskId) => request.get(`/tasks/${taskId}/status`)
export const approveTask = (taskId, payload) =>
  request.post(`/tasks/${taskId}/approve`, payload)
export const cancelTask = (taskId) => request.post(`/tasks/${taskId}/cancel`)
export const listTasks = (params) => request.get('/tasks', { params })

// ---------- 工作台 / 个人中心 ----------
export const fetchDashboard = () => request.get('/dashboard')
export const fetchMe = () => request.get('/auth/me')

// ---------- 审计日志(admin) ----------
export const listAuditLogs = (params) => request.get('/admin/audit-logs', { params })
export const listTaskEvents = (taskId) => request.get(`/admin/audit-logs/${taskId}/events`)
export const exportAuditLogs = (params) =>
  request.get('/admin/audit-logs/export', { params, responseType: 'blob' })

// ---------- 审批中心(approver/admin) ----------
export const listPendingApprovals = () => request.get('/approvals/pending')

// ---------- 用户管理(管理员) ----------
export const listUsers = () => request.get('/users')
export const createUser = (payload) => request.post('/users', payload)
export const resetUserPassword = (userId, newPassword) =>
  request.put(`/users/${userId}/password`, { new_password: newPassword })
export const deleteUser = (userId) => request.delete(`/users/${userId}`)
export const updateUserRoles = (userId, roles) => request.put(`/users/${userId}/roles`, { roles })

// ---------- 指标看板(管理员) ----------
export const fetchMetrics = () => request.get('/admin/metrics')

// ---------- 指标口径管理(管理员) ----------
export const listMetricDefinitions = () => request.get('/admin/metric-definitions')
export const createMetricDefinition = (payload) => request.post('/admin/metric-definitions', payload)
export const updateMetricDefinition = (metricId, payload) =>
  request.put(`/admin/metric-definitions/${metricId}`, payload)
export const deleteMetricDefinition = (metricId) => request.delete(`/admin/metric-definitions/${metricId}`)

// ---------- 数据权限(管理员) ----------
export const listDataPolicies = () => request.get('/admin/data-policies')
export const createDataPolicy = (payload) => request.post('/admin/data-policies', payload)
export const updateDataPolicy = (policyId, payload) =>
  request.put(`/admin/data-policies/${policyId}`, payload)
export const deleteDataPolicy = (policyId) => request.delete(`/admin/data-policies/${policyId}`)

// ---------- 数据源配置(管理员) ----------
export const listDataSources = () => request.get('/data-sources')
export const createDataSource = (payload) => request.post('/data-sources', payload)
export const updateDataSource = (sourceId, payload) =>
  request.put(`/data-sources/${sourceId}`, payload)
export const deleteDataSource = (sourceId) => request.delete(`/data-sources/${sourceId}`)
export const testDataSource = (payload) => request.post('/data-sources/test', payload)
export const fetchSchemaTables = (payload) => request.post('/admin/schema-tables', payload)

// ---------- 报告下载(blob 携带 Authorization, 避免原生 <a> 直连 401) ----------
export const downloadTaskReport = (taskId) =>
  request.get(`/tasks/${taskId}/download`, { responseType: 'blob' })

// ---------- 交互式看板 ----------
export const fetchBoard = (taskId) => request.get(`/tasks/${taskId}/board`)
export const fetchDrill = (taskId, params) => request.get(`/tasks/${taskId}/drill`, { params })

// ---------- 数据溯源(报告数字核验) ----------
export const fetchLineage = (taskId) => request.get(`/tasks/${taskId}/lineage`)
export const rerunQueryRun = (taskId, runId) =>
  request.post(`/tasks/${taskId}/query-runs/${runId}/rerun`)

// ---------- 定时任务(登录用户; 管理员可配推送范围) ----------
export const listScheduledTasks = () => request.get('/scheduled-tasks')
export const createScheduledTask = (payload) => request.post('/scheduled-tasks', payload)
export const updateScheduledTask = (taskId, payload) =>
  request.put(`/scheduled-tasks/${taskId}`, payload)
export const deleteScheduledTask = (taskId) => request.delete(`/scheduled-tasks/${taskId}`)
export const permanentApproval = (taskId, payload) =>
  request.post(`/admin/scheduled-tasks/${taskId}/permanent-approval`, payload)

// ---------- 站内通知(本人) ----------
export const listNotifications = (params) => request.get('/notifications', { params })
export const markNotificationsRead = (payload) => request.post('/notifications/read', payload)

// ---------- 数据字典(管理员) ----------
export const listSchemaDict = (params) => request.get('/admin/schema-dict', { params })
export const createSchemaDict = (payload) => request.post('/admin/schema-dict', payload)
export const updateSchemaDict = (itemId, payload) =>
  request.put(`/admin/schema-dict/${itemId}`, payload)
export const deleteSchemaDict = (itemId) => request.delete(`/admin/schema-dict/${itemId}`)
