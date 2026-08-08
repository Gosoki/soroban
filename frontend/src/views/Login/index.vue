<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <div class="head">
        <span class="logo">算</span>
        <div>
          <div class="title">算盤 soroban</div>
          <div class="sub">代购集运记账</div>
        </div>
      </div>
      <el-form :model="form" @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" size="large" :prefix-icon="User" style="width: 100%" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" size="large"
                    :prefix-icon="Lock" show-password style="width: 100%" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="submit">
          登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { authApi } from '@/api'

const router = useRouter()
const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const res = await authApi.login(form.username, form.password)
    localStorage.setItem('auth_token', res.access_token)
    localStorage.setItem('auth_user', JSON.stringify(res.user))
    router.push('/dashboard')
  } catch (_) {
    // 错误已由 http 拦截器统一提示
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap { height: 100vh; display: flex; align-items: center; justify-content: center; }
.login-card { width: 360px; }
.head { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
.logo {
  width: 44px; height: 44px; border-radius: 8px; background: var(--brand); color: #fff;
  font-size: 24px; font-weight: 700; display: flex; align-items: center; justify-content: center;
}
.title { font-size: 18px; font-weight: 600; color: var(--txt-1); }
.sub { font-size: 12px; color: var(--txt-3); }
</style>
