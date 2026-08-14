<template>
  <div class="chat-page">
    <!-- 会话侧栏: 新建/切换会话(旧会话保留在列表) -->
    <div class="session-sidebar">
      <div class="sidebar-header">
        <el-button type="primary" size="small" class="new-session-btn" @click="newSession">
          ＋ 新建会话
        </el-button>
      </div>
      <div class="session-list" v-loading="sessionLoading">
        <div
          v-for="s in chatStore.sessions"
          :key="s.id"
          class="session-item"
          :class="{ active: s.id === chatStore.currentSessionId, pinned: s.is_pinned }"
          @click="switchSession(s.id)"
        >
          <div class="s-title">
            <span v-if="s.is_pinned" class="pin-badge">📌</span>{{ s.title || '新对话' }}
          </div>
          <div class="s-meta">{{ fmtTime(s.updated_at) }} · {{ s.message_count }} 条</div>
          <el-dropdown
            trigger="click"
            class="session-more"
            @click.stop
            @command="(cmd) => onSessionCmd(cmd, s)"
          >
            <span class="more-btn" @click.stop>⋯</span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="pin">
                  {{ s.is_pinned ? '取消置顶' : '置顶' }}
                </el-dropdown-item>
                <el-dropdown-item command="rename">重命名</el-dropdown-item>
                <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div v-if="!chatStore.sessions.length" class="session-empty">
          暂无会话<br />点击"新建会话"开始
        </div>
      </div>

      <!-- 重命名会话 -->
      <el-dialog v-model="renameDialog" title="重命名会话" width="360px">
        <el-input
          v-model="renameVal"
          placeholder="输入新的会话名称"
          maxlength="100"
          show-word-limit
          @keyup.enter="confirmRename"
        />
        <template #footer>
          <el-button @click="renameDialog = false">取消</el-button>
          <el-button type="primary" @click="confirmRename">保存</el-button>
        </template>
      </el-dialog>
    </div>

    <!-- 聊天主区 -->
    <div class="chat-main">
      <!-- 顶部 -->
      <div class="chat-header">
        <div class="chat-title">
          <span class="bot-avatar">🤖</span>
          <div>
            <div class="t">数据分析助手</div>
            <div class="s">自然语言 → 自动化分析报告</div>
          </div>
        </div>
      </div>

    <!-- 消息区 -->
    <div ref="bodyRef" class="chat-body">
      <div v-for="m in chatStore.messages" :key="m.id" class="msg-row" :class="m.role">
        <div class="msg-avatar" :class="m.role">
          {{ m.role === 'user' ? '我' : '🤖' }}
        </div>
        <div class="bubble" :class="m.role">
          <!-- 用户文本 -->
          <template v-if="m.role === 'user'">{{ m.content }}</template>

          <!-- 助手文本(欢迎/普通消息) -->
          <template v-else-if="m.type === 'text'"><span style="white-space: pre-wrap">{{ m.content }}</span></template>

          <!-- 助手任务消息(进度/审批/报告) -->
          <template v-else-if="m.type === 'task'">
            <div class="task-query">📌 {{ m.query }}</div>

            <!-- 进行中: 进度条 -->
            <template v-if="['pending', 'running'].includes(m.status)">
              <el-progress
                :percentage="m.progressPercent || 0"
                :stroke-width="10"
                color="#4f46e5"
                :format="() => `${m.progressPercent || 0}%`"
              />
              <div class="task-progress">{{ m.progressDetail || m.status }}</div>
              <el-button v-if="m.status === 'running'" link type="danger" size="small" @click="onCancel(m)">
                🛑 取消任务
              </el-button>
            </template>

            <!-- 等待审批 -->
            <template v-else-if="m.status === 'awaiting_approval'">
              <el-alert
                type="warning"
                :closable="false"
                show-icon
                :title="canApprove ? '任务等待人工审批' : '已提交审批请求, 等待审批人处理'"
                style="margin-bottom: 8px"
              />
              <template v-if="canApprove">
                <el-input v-model="m._comment" placeholder="审批意见(可选)" size="small" style="margin-bottom: 8px" />
                <el-button type="success" size="small" @click="onApprove(m, true)">✅ 批准</el-button>
                <el-button type="danger" size="small" @click="onApprove(m, false)">❌ 拒绝</el-button>
              </template>
            </template>

            <!-- 完成: 按产出模式显示按钮(有看板才有看板按钮, 有PDF才有下载) -->
            <template v-else-if="m.status === 'completed'">
              <div class="report-body" v-html="renderMd(m.reportContent)" />
              <div v-if="m.hasPdf || m.hasBoard" style="margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap">
                <el-button v-if="m.resultPath && m.hasPdf" type="primary" size="small" @click="handleDownload(m.taskId)">
                  📄 下载报告(PDF)
                </el-button>
                <router-link v-if="m.hasBoard" :to="`/board/${m.taskId}`">
                  <el-button type="success" size="small" plain>📊 交互式看板</el-button>
                </router-link>
              </div>
              <div v-else-if="isAnswerOnly(m)" class="answer-only-tip">⚡ 简洁回答模式(未生成报告/看板/PDF)</div>
            </template>

            <!-- 失败 / 取消 -->
            <el-alert
              v-else-if="m.status === 'failed'"
              type="error"
              :closable="false"
              show-icon
              :title="'任务失败: ' + (m.errorLog || '')"
            />
            <el-alert v-else-if="m.status === 'canceled'" type="info" :closable="false" show-icon title="任务已取消" />
          </template>
        </div>
      </div>

      <!-- 思考中 -->
      <div v-if="thinking" class="msg-row assistant">
        <div class="msg-avatar assistant">🤖</div>
        <div class="bubble assistant typing">
          <span class="dot" /><span class="dot" /><span class="dot" />
        </div>
      </div>
    </div>

    <!-- 输入区 -->
    <div class="chat-input">
      <el-input
        v-model="draft"
        placeholder="输入数据分析需求, 例如: 统计最近7天各品类销售额, 回车发送"
        size="large"
        clearable
        @keyup.enter="send"
      />
      <el-button type="primary" size="large" :disabled="!draft.trim() || thinking" @click="send">
        发送
      </el-button>
    </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import { submitTask, fetchTaskStatus, approveTask, cancelTask } from '@/api'
import { handleDownload } from '@/utils/download'
import { useUserStore } from '@/stores/user'
import { useChatStore } from '@/stores/chat'

const store = useUserStore()
const canApprove = computed(() => store.isAdmin || store.roles.includes('approver'))

const chatStore = useChatStore()
const draft = ref('')
const thinking = ref(false)
const bodyRef = ref()
const sessionLoading = ref(false)

// 会话 ⋯ 菜单: 置顶 / 重命名 / 删除
const renameDialog = ref(false)
const renameVal = ref('')
const renameTarget = ref(null)

// 欢迎语(仅空会话时展示)
const WELCOME = {
  role: 'assistant',
  type: 'text',
  content:
    '你好!我是数据分析助手 🤖\n\n' +
    '直接输入你想分析的业务问题,我会自动完成:\n' +
    '1. 拆解分析需求,生成执行计划\n' +
    '2. 编写只读 SQL 查询业务数据(PostgreSQL)\n' +
    '3. 计算核心指标,支持环比/同比/趋势对比\n' +
    '4. 生成含图表、数据明细、行动建议的 PDF 分析报告\n\n' +
    '直接输入你的业务问题即可开始'
}

// 消息统一入口: 用户消息/任务消息落库, 助手文本也落库(会话可回放)
function push(msg) {
  if (msg.role === 'user') {
    chatStore.pushUser(msg.content)
  } else if (msg.type === 'task') {
    chatStore.pushTask(msg)
  } else {
    chatStore.pushText(msg.content)
  }
  scrollBottom()
}

// 任务消息: 直接放入数组(同一对象引用, poll 更新才能触发渲染)
function pushTask(taskMsg) {
  chatStore.pushTask(taskMsg)
  scrollBottom()
}

function scrollBottom() {
  nextTick(() => {
    if (bodyRef.value) bodyRef.value.scrollTop = bodyRef.value.scrollHeight
  })
}
watch(() => chatStore.messages, scrollBottom)

function renderMd(content) {
  if (!content) return ''
  return marked.parse(content, { async: false, breaks: true })
}

// 简洁问答模式检测: 报告内容以"直接回答"标题开头(与 reporter._answer_only_report 约定一致)
function isAnswerOnly(m) {
  return (m.reportContent || '').includes('直接回答')
}

// ---------- 发送与轮询 ----------
// 闲聊检测(与后端 src/utils/chat_gate.py 规则一致): 命中则本地直接回复, 不进流水线
const CHAT_WORDS = ['你好','您好','嗨','哈喽','hello','hi','hey','早上好','下午好','晚上好','在吗','在不在','谢谢','感谢','辛苦了','多谢','再见','拜拜','晚安','bye','你是谁','你叫什么','你能做什么','会做什么','介绍一下','哈哈','嗯嗯','好的','好的吧','ok','在干嘛','吃饭了吗','天气','心情','无聊','开心','真棒','不错','厉害','嗯','哦']
const ANALYSIS_HINTS = ['统计','销售额','查询','数据','报表','对比','趋势','占比','分析','明细','订单','品类','用户','留存','转化','环比','同比','最近','近7','近30','上周','本月','sql','数据库','销量','金额','多少','几个','几号','增长率','客单价','库存','毛利','图表','报告','看板','下钻']

function isChitchat(q) {
  const t = q.trim().toLowerCase()
  if (!t) return true
  if (ANALYSIS_HINTS.some((h) => t.includes(h))) return false
  if (t.length < 2) return true
  return CHAT_WORDS.some((w) => t.includes(w))
}

async function send() {
  const query = draft.value.trim()
  if (!query || thinking.value) return
  draft.value = ''

  // 闲聊即时回复: 不创建任务, 直接以助手消息回复
  if (isChitchat(query)) {
    push({ role: 'user', content: query })
    push({
      role: 'assistant',
      type: 'text',
      content: '你好呀！我是数据分析助手 🤖\n\n我只擅长数据分析, 例如:\n- 统计最近7天各品类销售额, 对比上周变化\n- 分析近30天销售趋势\n- 查询各品类订单量与客单价\n\n请直接输入你的业务问题, 我来帮你分析～'
    })
    return
  }

  push({ role: 'user', content: query })
  const taskMsg = reactive({
    role: 'assistant',
    type: 'task',
    query,
    status: 'pending',
    progress: '',
    progressDetail: '提交中...',
    progressPercent: 5,
    errorLog: '',
    reportContent: '',
    resultPath: '',
    _comment: '',
    _timer: null
  })
  pushTask(taskMsg)

  thinking.value = true
  try {
    const data = await submitTask(query, chatStore.currentSessionId)
    taskMsg.taskId = data.task_id
    taskMsg.progressDetail = '任务已提交, 正在启动流水线...'
    poll(taskMsg)
  } catch (e) {
    // 后端闲聊拦截兜底: 422 + detail 以 CHAT_REPLY:: 开头 -> 以助手消息展示回复
    const detail = e.response?.data?.detail || ''
    if (e.response?.status === 422 && detail.startsWith('CHAT_REPLY::')) {
      taskMsg.status = 'completed'
      taskMsg.reportContent = detail.slice('CHAT_REPLY::'.length).replace(/\n/g, '\n\n')
      return
    }
    taskMsg.status = 'failed'
    taskMsg.errorLog = '任务提交失败'
  } finally {
    thinking.value = false
  }
}

function poll(m) {
  clearTimeout(m._timer)
  ;(async () => {
    let data = null
    try {
      data = await fetchTaskStatus(m.taskId)
    } catch (e) {
      // 请求失败不终止轮询, 3s 后重试
      m._timer = setTimeout(() => poll(m), 3000)
      return
    }
    m.status = data.status
    m.progress = data.progress || ''
    m.progressDetail = data.progress_detail || data.progress || m.progressDetail
    m.progressPercent = data.progress_percent || m.progressPercent
    m.errorLog = data.error_log || ''
    m.reportContent = data.report_content || ''
    m.resultPath = data.result_path || ''
    m.hasPdf = !!data.has_pdf
    m.hasBoard = !!data.has_board

    if (['pending', 'running', 'awaiting_approval'].includes(m.status)) {
      // awaiting_approval 也继续轮询: 审批人批准后任务会恢复执行, 需自动跟进
      m._timer = setTimeout(() => poll(m), 2000)
    } else {
      // 终态(completed/failed/canceled): 任务消息落库, 历史会话可回放
      chatStore.persistTask(m)
    }
  })()
}

// ---------- 审批 / 取消 ----------
async function onApprove(m, approved) {
  try {
    await approveTask(m.taskId, {
      approved,
      approver: store.username || 'approver',
      comment: (m._comment || '').trim()
    })
    ElMessage.success(approved ? '已批准, 任务继续执行' : '已拒绝')
    poll(m)
  } catch (e) {
    /* 已提示 */
  }
}

async function onCancel(m) {
  try {
    await cancelTask(m.taskId)
    ElMessage.warning('任务已取消')
    poll(m)
  } catch (e) {
    /* 已提示 */
  }
}

// ---------- 初始化 ----------
// 多会话: 加载会话列表 -> 恢复上次会话; 空会话才展示欢迎语
onMounted(async () => {
  sessionLoading.value = true
  try {
    await chatStore.init()
  } finally {
    sessionLoading.value = false
  }
  if (chatStore.messages.length === 0) {
    push(WELCOME)
  } else {
    resumePollingForActiveTasks()
  }
})

// ---------- 会话操作 ----------
async function newSession() {
  sessionLoading.value = true
  try {
    await chatStore.createSession()
  } finally {
    sessionLoading.value = false
  }
  push(WELCOME)
}

async function switchSession(id) {
  if (id === chatStore.currentSessionId) return
  // 停止旧会话进行中任务的轮询(防止切换后后台泄漏)
  chatStore.messages.forEach((m) => {
    if (m._timer) {
      clearTimeout(m._timer)
      m._timer = null
    }
  })
  sessionLoading.value = true
  try {
    await chatStore.switchSession(id)
    resumePollingForActiveTasks()
  } finally {
    sessionLoading.value = false
  }
}

// 历史消息恢复轮询: 刷新/切会话后, 对仍 pending/running/awaiting_approval 的
// 任务消息重新开始轮询, 避免"审批中心已拒绝, 聊天页仍显示待审批"的状态滞留
function resumePollingForActiveTasks() {
  chatStore.messages.forEach((m) => {
    if (m.type === 'task' && ['pending', 'running', 'awaiting_approval'].includes(m.status) && !m._timer) {
      poll(m)
    }
  })
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---------- 会话 ⋯ 菜单 ----------
function onSessionCmd(cmd, s) {
  if (cmd === 'pin') pinSession(s)
  else if (cmd === 'rename') {
    renameTarget.value = s
    renameVal.value = s.title && s.title !== '新对话' ? s.title : ''
    renameDialog.value = true
  } else if (cmd === 'delete') removeSession(s)
}

async function pinSession(s) {
  try {
    await chatStore.updateSession(s.id, { is_pinned: !s.is_pinned })
    ElMessage.success(s.is_pinned ? '已取消置顶' : '已置顶')
  } catch (e) {
    /* 已提示 */
  }
}

async function confirmRename() {
  if (!renameVal.value.trim()) {
    ElMessage.warning('名称不能为空')
    return
  }
  try {
    await chatStore.updateSession(renameTarget.value.id, { title: renameVal.value.trim() })
    renameDialog.value = false
    ElMessage.success('已重命名')
  } catch (e) {
    /* 已提示 */
  }
}

async function removeSession(s) {
  try {
    await ElMessageBox.confirm(
      `确定删除会话「${s.title || '新对话'}」吗?\n会话内的消息会被删除, 但已触发的分析任务仍完整保留在任务历史中。`,
      '删除会话',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch (e) {
    return // 用户取消
  }
  try {
    await chatStore.deleteSession(s.id)
    ElMessage.success('会话已删除')
  } catch (e) {
    /* 已提示 */
  }
}
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: row;
  height: calc(100vh - 56px - 40px); /* 减顶栏与 main padding */
  background: #fff;
  border: 1px solid #eceff5;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06);
  overflow: hidden;
}

/* 会话侧栏 */
.session-sidebar {
  width: 236px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: #f7f8fa;
  border-right: 1px solid #eceff5;
}
.sidebar-header {
  padding: 12px;
  border-bottom: 1px solid #eceff5;
}
.new-session-btn {
  width: 100%;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.session-item {
  position: relative;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
}
.session-item:hover {
  background: #eef1f6;
}
.session-item.active {
  background: #e8f0fe;
  border-color: #c6d8f7;
}
.session-item.pinned {
  background: #fdf6e3;
}
.s-title {
  font-size: 13px;
  color: #1f2329;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 22px;
}
.pin-badge {
  margin-right: 2px;
}
.s-meta {
  font-size: 11px;
  color: #8a919f;
  margin-top: 3px;
}
.session-more {
  position: absolute;
  top: 8px;
  right: 6px;
}
.more-btn {
  display: inline-block;
  width: 22px;
  height: 22px;
  line-height: 20px;
  text-align: center;
  border-radius: 4px;
  font-size: 15px;
  color: #8a919f;
  cursor: pointer;
}
.more-btn:hover {
  background: #e0e5ec;
  color: #1f2329;
}
.session-empty {
  padding: 24px 8px;
  text-align: center;
  color: #b0b6c0;
  font-size: 12px;
  line-height: 1.8;
}

/* 聊天主区 */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.chat-header {
  padding: 14px 20px;
  border-bottom: 1px solid #eceff5;
  background: #fafbfc;
}
.chat-title { display: flex; align-items: center; gap: 10px; }
.bot-avatar { font-size: 26px; }
.chat-title .t { font-weight: 700; }
.chat-title .s { font-size: 12px; color: #6b7280; }

.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 18px 20px;
  background: #f6f7fb;
}
.msg-row { display: flex; margin-bottom: 16px; align-items: flex-start; gap: 10px; }
.msg-row.user { flex-direction: row-reverse; }
.msg-avatar {
  width: 34px; height: 34px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700; flex-shrink: 0;
}
.msg-avatar.user { background: linear-gradient(135deg, #4f46e5, #7c3aed); color: #fff; }
.msg-avatar.assistant { background: #eef2ff; }

.bubble {
  max-width: 72%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}
.bubble.user {
  background: linear-gradient(135deg, #4f46e5, #5b53ea);
  color: #fff;
  border-top-right-radius: 2px;
}
.bubble.assistant {
  background: #fff;
  border: 1px solid #eceff5;
  border-top-left-radius: 2px;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.05);
}
.task-query { font-weight: 600; margin-bottom: 8px; }
.task-progress { margin-top: 8px; color: #6b7280; font-size: 13px; }

/* 报告渲染 */
.report-body { font-size: 13px; max-height: 420px; overflow: auto; margin-bottom: 8px; }
.answer-only-tip { font-size: 12px; color: #9ca3af; margin-top: 4px; }
.report-body :deep(h1), .report-body :deep(h2) { font-size: 15px; margin: 10px 0 6px; }
.report-body :deep(h3) { font-size: 14px; }
.report-body :deep(table) { border-collapse: collapse; width: 100%; }
.report-body :deep(td), .report-body :deep(th) { border: 1px solid #e5e7eb; padding: 4px 8px; }
.report-body :deep(code) { background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }
.report-body :deep(img) { max-width: 100%; }

/* 思考动画 */
.typing { display: flex; gap: 4px; padding: 14px; }
.dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #9ca3af; animation: blink 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }

.chat-input {
  display: flex;
  gap: 10px;
  padding: 14px 20px;
  border-top: 1px solid #eceff5;
  background: #fff;
}
.chat-input .el-input { flex: 1; }
</style>
