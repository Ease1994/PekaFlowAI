import { useEffect, useState } from 'react'
import { Spin } from 'antd'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import MainLayout from './layouts/MainLayout'
import Login from './pages/Login'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'
import Dashboard from './pages/Dashboard'
import Projects from './pages/Projects'
import ProjectDetail from './pages/ProjectDetail'
import PipelineEditor from './pages/PipelineEditor'
import Releases from './pages/Releases'
import ExecutionHistory from './pages/ExecutionHistory'
import ExecutionDetail from './pages/ExecutionDetail'
import Agents from './pages/Agents'
import Nodes from './pages/Nodes'
import DeployRequests from './pages/DeployRequests'
import Artifacts from './pages/Artifacts'
import Store from './pages/Store'
import Credentials from './pages/Credentials'
import AiChat from './pages/AiChat'
import Settings from './pages/Settings'
import ModelConfig from './pages/ModelConfig'
import Users from './pages/Users'
import PermissionManagement from './pages/PermissionManagement'
import ReleaseApproval from './pages/ReleaseApproval'
import PmWorkbench from './pages/PmWorkbench'
import ApiHandbook from './pages/ApiHandbook'
import { NoticeCenter } from './modules/notify'
import { useAuthStore } from './stores/auth'
import { ApplyPlatformBranding } from './hooks/usePlatformBranding'
import { canSeeMenu, type MenuCatalog } from './menus'
import { useQuery } from '@tanstack/react-query'
import { get } from './api/client'
import type { UserInfo } from './api/types'

/**
 * 登录态守卫。有 token 时先拉一次 /me，把本地缓存的 is_admin 换成库里的值。
 * 后台刚把 LDAP 用户设成管理员时，不先同步就会被 RequireAdmin 踢回首页。
 */
function RequireAuth({ children }: { children: JSX.Element }) {
  const token = useAuthStore((s) => s.token)
  const setUser = useAuthStore((s) => s.setUser)
  const loc = useLocation()
  const [ready, setReady] = useState(!token)

  useEffect(() => {
    if (!token) {
      setReady(true)
      return
    }
    let cancelled = false
    setReady(false)
    get<UserInfo>('/auth/me', undefined, { skipErrorToast: true })
      .then((u) => {
        if (!cancelled) setUser(u)
      })
      .catch(() => {
        // 401 由拦截器清会话；其它错误先用本地缓存，不挡进站
      })
      .finally(() => {
        if (!cancelled) setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [token, setUser])

  if (!token) {
    const next = encodeURIComponent(loc.pathname + loc.search)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  if (!ready) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
        <Spin />
      </div>
    )
  }
  return children
}

function RequireAdmin({ children }: { children: JSX.Element }) {
  const user = useAuthStore((s) => s.user)
  if (!user?.is_admin) return <Navigate to="/" replace />
  return children
}

function RequireMenu({ menu, children }: { menu: string; children: JSX.Element }) {
  const user = useAuthStore((s) => s.user)
  const { data } = useQuery({
    queryKey: ['menus'],
    queryFn: () => get<MenuCatalog>('/menus'),
    retry: false,
  })
  if (!canSeeMenu(data, menu, !!user?.is_admin)) return <Navigate to="/projects" replace />
  return children
}

function HomePage() {
  const user = useAuthStore((s) => s.user)
  return user?.is_admin ? <Dashboard /> : <Navigate to="/projects" replace />
}

export default function App() {
  return (
    <>
      <ApplyPlatformBranding />
      <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <MainLayout />
          </RequireAuth>
        }
      >
        <Route index element={<HomePage />} />
        <Route path="projects" element={<Projects />} />
        <Route path="projects/:projectId" element={<ProjectDetail />} />
        <Route path="pipeline/:pipelineId/edit" element={<PipelineEditor />} />
        <Route
          path="releases"
          element={
            <RequireAdmin>
              <Releases />
            </RequireAdmin>
          }
        />
        <Route path="executions/:pipelineId" element={<ExecutionHistory />} />
        <Route path="executions/:pipelineId/:releaseId" element={<ExecutionDetail />} />
        <Route path="executions/:pipelineId" element={<ExecutionHistory />} />
        <Route path="executions/:pipelineId/:releaseId" element={<ExecutionDetail />} />
        <Route path="agents" element={<RequireMenu menu="agents"><Agents /></RequireMenu>} />
        <Route path="nodes" element={<RequireMenu menu="nodes"><Nodes /></RequireMenu>} />
        <Route path="pm" element={<PmWorkbench />} />
        <Route path="deploy-requests" element={<DeployRequests />} />
        <Route path="artifacts" element={<RequireMenu menu="artifacts"><Artifacts /></RequireMenu>} />
        <Route path="skills" element={<RequireMenu menu="skills"><Store /></RequireMenu>} />
        <Route path="store" element={<Navigate to="/skills" replace />} />
        <Route path="credentials" element={<RequireMenu menu="credentials"><Credentials /></RequireMenu>} />
        <Route path="ai" element={<RequireMenu menu="ai"><AiChat /></RequireMenu>} />
        <Route path="handbook" element={<RequireMenu menu="handbook"><ApiHandbook /></RequireMenu>} />
        <Route path="approvals" element={<ReleaseApproval />} />
        <Route path="notifications" element={<NoticeCenter />} />
        <Route path="access" element={<Navigate to="/permissions?tab=apply" replace />} />
        <Route path="models" element={<RequireMenu menu="models"><ModelConfig /></RequireMenu>} />
        <Route
          path="settings"
          element={
            <RequireAdmin>
              <Settings />
            </RequireAdmin>
          }
        />
        <Route
          path="users"
          element={
            <RequireAdmin>
              <Users />
            </RequireAdmin>
          }
        />
        <Route path="permissions" element={<PermissionManagement />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  )
}
