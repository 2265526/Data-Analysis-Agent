// 多会话 store: 会话列表 + 消息(后端权威存储)
// 设计(调研 open-webui/NextChat):
// - 会话与消息由后端持久化(chat_sessions/chat_messages 表), 跨设备/刷新不丢失
// - 新旧会话隔离: 后端按 owner 过滤; 旧会话保留在列表, 可切换查看
// - 本地仅缓存"当前会话 id"用于下次进入恢复
// - 消息本地即时渲染(乐观 UI), 同时异步写入后端(fire-and-forget)
import { defineStore } from 'pinia'
import request from '@/api/request'

const LS_SESSION_KEY = 'chat_current_session_v1'
const MAX_MESSAGES = 80

// 后端消息 -> 前端展示结构(Index.vue 用 m.role + m.type 渲染)
function toLocal(m) {
  const base = {
    id: m.id,
    time: m.created_at,
    content: m.content || ''
  }
  if (m.role === 'user') {
    return { ...base, role: 'user' }
  }
  if (m.type === 'task') {
    return {
      ...base,
      role: 'assistant',
      type: 'task',
      taskId: m.task_id,
      // 后端按任务当前状态动态返回(历史消息可能仍待审批/进行中, 不能写死 completed)
      status: m.status || 'completed',
      reportContent: m.content || '',
      hasPdf: !!m.has_pdf,
      hasBoard: !!m.has_board
    }
  }
  return { ...base, role: 'assistant', type: 'text' }
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [], // [{id, title, message_count, updated_at}]
    currentSessionId: null,
    messages: [], // 当前会话消息(后端为权威)
    loaded: false
  }),
  actions: {
    async init() {
      // 启动: 加载会话列表 -> 恢复上次会话(或最近会话/新建)
      if (this.loaded) return
      await this.refreshSessions()
      const saved = localStorage.getItem(LS_SESSION_KEY)
      if (saved && this.sessions.some((s) => String(s.id) === saved)) {
        await this.switchSession(Number(saved))
      } else if (this.sessions.length > 0) {
        await this.switchSession(this.sessions[0].id) // 最近的会话
      } else {
        await this.createSession()
      }
      this.loaded = true
    },
    async refreshSessions() {
      try {
        const data = await request.get('/chat/sessions')
        this.sessions = data.sessions || []
      } catch (e) {
        /* 后端不可用时保持现状 */
      }
    },
    async createSession() {
      const s = await request.post('/chat/sessions')
      this.sessions.unshift(s)
      await this.switchSession(s.id)
      return s
    },
    async switchSession(id) {
      this.currentSessionId = id
      try {
        localStorage.setItem(LS_SESSION_KEY, String(id))
      } catch (e) {
        /* ignore */
      }
      this.messages = []
      try {
        const data = await request.get(`/chat/sessions/${id}/messages`)
        this.messages = (data.messages || []).map(toLocal).slice(-MAX_MESSAGES)
      } catch (e) {
        /* 后端不可用: 空会话 */
      }
    },
    async updateSession(id, payload) {
      // 更新会话(置顶/重命名), 本地列表同步
      const updated = await request.patch(`/chat/sessions/${id}`, payload)
      const idx = this.sessions.findIndex((s) => s.id === id)
      if (idx >= 0) {
        this.sessions[idx] = { ...this.sessions[idx], ...updated }
        this.sessions.sort((a, b) => {
          if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
          return String(b.updated_at || '').localeCompare(String(a.updated_at || ''))
        })
      }
      return updated
    },
    async deleteSession(id) {
      // 删除会话及其消息(后端级联); 已触发任务独立保留在任务历史
      await request.delete(`/chat/sessions/${id}`)
      this.sessions = this.sessions.filter((s) => s.id !== id)
      if (this.currentSessionId === id) {
        this.currentSessionId = null
        this.messages = []
        try {
          localStorage.removeItem(LS_SESSION_KEY)
        } catch (e) {
          /* ignore */
        }
        if (this.sessions.length) {
          await this.switchSession(this.sessions[0].id)
        } else {
          await this.createSession()
        }
      }
    },
    // ---- 消息写入(乐观渲染 + 异步落库) ----
    pushUser(content) {
      if (this.currentSessionId == null) return null
      const msg = {
        id: `u${Date.now()}`,
        role: 'user',
        content,
        time: new Date().toISOString()
      }
      this.messages.push(msg)
      this._trim()
      // 异步落库(首条消息触发后端自动生成标题)
      request
        .post(`/chat/sessions/${this.currentSessionId}/messages`, {
          role: 'user',
          type: 'text',
          content
        })
        .then(() => this.refreshSessions())
        .catch(() => {})
      return msg
    },
    pushText(content) {
      // 助手文本消息(闲聊回复/欢迎语): 本地渲染 + 异步落库
      if (this.currentSessionId == null) return null
      const msg = {
        id: `a${Date.now()}`,
        role: 'assistant',
        type: 'text',
        content,
        time: new Date().toISOString()
      }
      this.messages.push(msg)
      this._trim()
      request
        .post(`/chat/sessions/${this.currentSessionId}/messages`, {
          role: 'assistant',
          type: 'text',
          content
        })
        .catch(() => {})
      return msg
    },
    pushTask(taskMsg) {
      // taskMsg: {taskId, status, reportContent, hasPdf, hasBoard, errorLog}
      // 注意: 必须复用传入对象本身(不能展开副本), Index.vue 的 poll 会更新同一引用
      if (this.currentSessionId == null) return null
      taskMsg.id = `t${Date.now()}`
      taskMsg.time = new Date().toISOString()
      this.messages.push(taskMsg)
      this._trim()
      return taskMsg
    },
    persistTask(taskMsg) {
      // 任务终态落库(由 poll 在 completed 时调用, 幂等); 历史会话可原样回放
      if (this.currentSessionId == null) return
      if (taskMsg._persisted) return
      if (taskMsg.status === 'completed' && taskMsg.reportContent) {
        taskMsg._persisted = true
        request
          .post(`/chat/sessions/${this.currentSessionId}/messages`, {
            role: 'assistant',
            type: 'task',
            content: taskMsg.reportContent,
            task_id: taskMsg.taskId,
            has_pdf: !!taskMsg.hasPdf,
            has_board: !!taskMsg.hasBoard
          })
          .then(() => this.refreshSessions())
          .catch(() => {})
      }
    },
    _trim() {
      if (this.messages.length > MAX_MESSAGES) {
        this.messages = this.messages.slice(-MAX_MESSAGES)
      }
    }
  }
})
