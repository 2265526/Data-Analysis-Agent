import { defineStore } from 'pinia'
import { login as apiLogin } from '@/api'

export const useUserStore = defineStore('user', {
  state: () => ({
    token: localStorage.getItem('dp_token') || '',
    username: localStorage.getItem('dp_user') || '',
    roles: JSON.parse(localStorage.getItem('dp_roles') || '[]')
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    isAdmin: (s) => s.roles.includes('admin'),
    roleText: (s) => (s.roles.length ? s.roles.join(' / ') : '-')
  },
  actions: {
    async login(username, password) {
      const data = await apiLogin(username, password)
      this.token = data.access_token
      this.username = username
      this.roles = data.user?.roles || []
      localStorage.setItem('dp_token', this.token)
      localStorage.setItem('dp_user', this.username)
      localStorage.setItem('dp_roles', JSON.stringify(this.roles))
    },
    logout() {
      this.token = ''
      this.username = ''
      this.roles = []
      localStorage.removeItem('dp_token')
      localStorage.removeItem('dp_user')
      localStorage.removeItem('dp_roles')
    }
  }
})
