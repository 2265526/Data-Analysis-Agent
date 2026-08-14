<template>
  <div class="err-page">
    <div class="code">403</div>
    <h2>没有权限</h2>
    <p>您没有权限访问该页面,如有需要请联系管理员</p>
    <el-button type="primary" @click="$router.push('/dashboard')">返回工作台</el-button>
  </div>
</template>

<style scoped>
.err-page { text-align: center; padding: 80px 20px; }
.code { font-size: 72px; font-weight: 800; color: #ef4444; line-height: 1; }
h2 { margin: 16px 0 6px; }
p { color: #6b7280; margin-bottom: 20px; }
</style>
