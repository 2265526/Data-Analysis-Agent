<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <div class="badge">Data Pipeline Agent</div>
        <h1>数据分析智能体平台</h1>
        <p>企业内部 · 自然语言 → 自动化分析报告</p>
      </div>

      <el-segmented v-model="role" :options="['用户登录', '管理员登录']" block />

      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="onSubmit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" :prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
            :prefix-icon="Lock"
          />
        </el-form-item>
        <el-button type="primary" class="submit-btn" :loading="loading" @click="onSubmit">
          登 录
        </el-button>
      </el-form>

      <div class="tips">
        普通用户由管理员创建,密码遗忘请联系管理员重置
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const store = useUserStore()

const role = ref('用户登录')
const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

// 切换身份不预填账号, 由用户自行输入

async function onSubmit() {
  await formRef.value.validate()
  loading.value = true
  try {
    await store.login(form.username.trim(), form.password)
    ElMessage.success('登录成功')
    // 管理员默认进入用户管理, 普通用户进入数据分析
    const target = route.query.redirect || (store.isAdmin ? '/dashboard' : '/analysis')
    router.push(target)
  } catch (e) {
    /* 错误提示已由拦截器处理 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(1200px 500px at 20% -10%, rgba(79, 70, 229, 0.12), transparent 60%),
    radial-gradient(1000px 500px at 90% 110%, rgba(14, 165, 233, 0.10), transparent 55%),
    #f5f6fa;
  padding: 20px;
}
.login-card {
  width: 420px;
  background: #fff;
  border: 1px solid #eceff5;
  border-radius: 16px;
  box-shadow: 0 12px 40px rgba(16, 24, 40, 0.08);
  padding: 36px 40px 28px;
}
.brand { text-align: center; margin-bottom: 22px; }
.badge {
  display: inline-block;
  background: #eef2ff;
  color: #4f46e5;
  font-weight: 700;
  font-size: 12px;
  padding: 4px 14px;
  border-radius: 999px;
}
.brand h1 { font-size: 21px; margin: 14px 0 6px; letter-spacing: -0.01em; }
.brand p { margin: 0; color: #6b7280; font-size: 13px; }
.login-card :deep(.el-segmented) { margin-bottom: 22px; }
.submit-btn { width: 100%; margin-top: 4px; }
.tips {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px dashed #e5e7eb;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.7;
}
.tips code {
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 4px;
  color: #4f46e5;
  font-weight: 600;
}
</style>
