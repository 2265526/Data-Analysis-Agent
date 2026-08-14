import { ElMessage } from 'element-plus'
import { downloadTaskReport } from '@/api'

/**
 * 解析 Content-Disposition 中的文件名。
 * 优先 filename*=UTF-8''... 语法, 退而 filename="..."; 均缺失时返回空串。
 */
export function filenameFromDisposition(disposition) {
  if (!disposition) return ''
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (star) {
    try {
      return decodeURIComponent(star[1].replace(/["']/g, ''))
    } catch {
      /* fallthrough to plain filename */
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain ? plain[1] : ''
}

/**
 * 下载任务报告(带鉴权)。
 *
 * 背景: 原生 <a href="/api/v1/tasks/{id}/download" download> 直连不带 Authorization
 * header, oauth2 模式下后端必然返回 401; 故改用 axios(responseType: 'blob') 复用
 * 统一拦截器携带 Bearer token, 再通过 URL.createObjectURL 触发浏览器下载
 * (MDN: 使用后必须 revokeObjectURL 释放, 避免内存泄漏)。
 *
 * 401/403/404 等错误已由 request.js 拦截器统一提示并(401)跳转登录, 这里只兜底
 * 非 HTTP 错误与后端偶发返回 JSON 错误体的场景。
 */
export async function handleDownload(taskId) {
  try {
    const resp = await downloadTaskReport(taskId)
    const blob = resp.data
    if (blob?.type && blob.type.includes('application/json')) {
      // 兜底: 后端异常时可能返回 JSON 错误而非文件字节
      const text = await blob.text()
      let msg = '下载失败'
      try { msg = text ? (JSON.parse(text).detail || text) : msg } catch { msg = text || msg }
      throw new Error(msg)
    }
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filenameFromDisposition(resp.headers?.['content-disposition']) || `report-${taskId}.pdf`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    // 有 error.response 的 HTTP 错误已由拦截器提示过, 这里避免重复提示
    if (!e.response) ElMessage.error(e.message || '下载失败')
  }
}
