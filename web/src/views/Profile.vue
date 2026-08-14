<template>
  <div>
    <div class="page-header">
      <h2>👤 个人中心</h2>
      <p>账号信息(密码由管理员统一管理,如需重置请联系管理员)</p>
    </div>

    <el-row :gutter="16">
      <el-col :span="8">
        <div class="panel-card" style="text-align: center">
          <el-avatar :size="72" class="avatar">{{ (info.name || '?').charAt(0).toUpperCase() }}</el-avatar>
          <h3 style="margin: 12px 0 4px">{{ info.name }}</h3>
          <div>
            <el-tag v-for="r in info.roles" :key="r" size="small" style="margin: 0 4px">{{ r }}</el-tag>
          </div>
        </div>
      </el-col>
      <el-col :span="16">
        <div class="panel-card">
          <el-descriptions title="账号信息" :column="2" border>
            <el-descriptions-item label="用户 ID">{{ info.id }}</el-descriptions-item>
            <el-descriptions-item label="用户名">{{ info.name }}</el-descriptions-item>
            <el-descriptions-item label="角色">{{ (info.roles || []).join(' / ') || '-' }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ fmtTime(info.created_at) }}</el-descriptions-item>
            <el-descriptions-item label="更新时间">{{ fmtTime(info.updated_at) }}</el-descriptions-item>
          </el-descriptions>
          <el-alert
            style="margin-top: 14px"
            title="密码管理"
            description="内部项目采用管理员统一管控:如需修改密码,请联系管理员在「用户管理」中重置。"
            type="info"
            show-icon
            :closable="false"
          />
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { fetchMe } from '@/api'

const info = ref({ name: '', roles: [] })

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '-'
}

onMounted(async () => {
  try {
    info.value = await fetchMe()
  } catch (e) {
    /* 已提示 */
  }
})
</script>

<style scoped>
.avatar {
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  color: #fff;
  font-size: 28px;
  font-weight: 700;
}
</style>
