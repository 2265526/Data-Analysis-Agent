import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useUserStore } from '@/stores/user'

// 开发环境走 Vite 代理; 生产环境由 FastAPI 同源托管, 无需 baseURL
const request = axios.create({
  baseURL: '/api/v1',
  timeout: 30000
})

request.interceptors.request.use((config) => {
  const store = useUserStore()
  if (store.token) {
    config.headers.Authorization = `Bearer ${store.token}`
  }
  return config
})

request.interceptors.response.use(
  (resp) => {
    // blob 下载需保留完整 response(读取 Content-Disposition 文件名), 其余请求剥壳取 data
    if (resp.config.responseType === 'blob') return resp
    return resp.data
  },
  async (error) => {
    const status = error.response?.status
    // blob 模式下错误响应体也是 Blob, 需异步解析出 detail 文本(见 axios 官方 issue #5270)
    let detail = error.response?.data?.detail
    if (error.response?.data instanceof Blob) {
      try {
        const text = await error.response.data.text()
        if (text) {
          try { detail = JSON.parse(text).detail || text } catch { detail = text }
        } else {
          detail = '请求失败'
        }
      } catch {
        detail = '请求失败'
      }
    }
    if (status === 401) {
      const store = useUserStore()
      store.logout()
      ElMessage.error(typeof detail === 'string' ? detail : '登录已过期, 请重新登录')
      router.push('/login')
    } else if (status === 403) {
      ElMessage.error(typeof detail === 'string' ? detail : '没有权限执行该操作')
    } else if (status === 422) {
      ElMessage.error(typeof detail === 'string' ? detail : '参数不合法')
    } else if (status === 409) {
      ElMessage.error(typeof detail === 'string' ? detail : '资源已存在')
    } else if (status === 404) {
      ElMessage.error(typeof detail === 'string' ? detail : '资源不存在')
    } else {
      ElMessage.error(error.message || '请求失败')
    }
    return Promise.reject(error)
  }
)

export default request
