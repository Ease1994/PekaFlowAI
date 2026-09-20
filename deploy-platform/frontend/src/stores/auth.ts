import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { UserInfo } from '@/api/types'

interface AuthState {
  token: string | null
  user: UserInfo | null
  setAuth: (token: string, user: UserInfo) => void
  /** 只更新资料（管理员补邮箱、刷新 /me），不换 token */
  setUser: (user: UserInfo) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      setAuth: (token, user) => set({ token, user }),
      setUser: (user) => set({ user }),
      logout: () => set({ token: null, user: null }),
    }),
    { name: 'deploy-auth' },
  ),
)
