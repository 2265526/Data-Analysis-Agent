<template>
  <div class="err-page">
    <div class="code">404</div>
    <h2>页面不存在</h2>
    <p>您访问的页面不存在或已被移除</p>
    <el-button type="primary" @click="$router.push('/dashboard')">返回工作台</el-button>
  </div>
</template>

<style scoped>
.err-page { text-align: center; padding: 80px 20px; }
.code { font-size: 72px; font-weight: 800; color: #4f46e5; line-height: 1; }
h2 { margin: 16px 0 6px; }
p { color: #6b7280; margin-bottom: 20px; }
</style>
