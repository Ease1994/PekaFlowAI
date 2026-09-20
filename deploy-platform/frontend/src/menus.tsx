/**
 * 侧栏菜单目录。key 必须和后端 MENU_ITEMS、路由 path 一致。
 * 可见范围由 GET /menus 的 visible 决定；资源菜单还可按用户取消。
 */
import type { ReactNode } from 'react'
import {
  AuditOutlined,
  DashboardOutlined,
  ProjectOutlined,
  CloudServerOutlined,
  ShoppingOutlined,
  SafetyCertificateOutlined,
  RobotOutlined,
  RocketOutlined,
  SettingOutlined,
  KeyOutlined,
  ApiOutlined,
  DesktopOutlined,
  FileTextOutlined,
  InboxOutlined,
  ScheduleOutlined,
  UserOutlined,
  BookOutlined,
} from '@ant-design/icons'

/** 后端返回的一项菜单。audience: 当前生效的可见范围。 */
export interface MenuNode {
  key: string
  label: string
  group: string
  group_label: string
  audience: 'all' | 'admin'
  locked: boolean
}

export interface MenuCatalog {
  items: MenuNode[]
  visible: string[]
}

export const MENU_ICONS: Record<string, ReactNode> = {
  projects: <ProjectOutlined />,
  pm: <ScheduleOutlined />,
  'deploy-requests': <FileTextOutlined />,
  approvals: <AuditOutlined />,
  artifacts: <InboxOutlined />,
  agents: <CloudServerOutlined />,
  nodes: <DesktopOutlined />,
  skills: <ShoppingOutlined />,
  credentials: <SafetyCertificateOutlined />,
  models: <ApiOutlined />,
  ai: <RobotOutlined />,
  handbook: <BookOutlined />,
  dashboard: <DashboardOutlined />,
  releases: <RocketOutlined />,
  users: <UserOutlined />,
  permissions: <KeyOutlined />,
  settings: <SettingOutlined />,
}

/** 接口失败时：工作台 + 资源对全员，系统管理仍仅管理员。 */
export const FALLBACK_MENU_ITEMS: MenuNode[] = [
  { key: 'projects', label: '项目与流水线', group: 'workbench', group_label: '工作台', audience: 'all', locked: true },
  { key: 'pm', label: '项目工作台', group: 'workbench', group_label: '工作台', audience: 'all', locked: true },
  { key: 'deploy-requests', label: '发布计划', group: 'workbench', group_label: '工作台', audience: 'all', locked: true },
  { key: 'approvals', label: '发布审批', group: 'workbench', group_label: '工作台', audience: 'all', locked: true },
  { key: 'permissions', label: '权限管理', group: 'workbench', group_label: '工作台', audience: 'all', locked: true },
  { key: 'artifacts', label: '制品库', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'agents', label: '构建机管理', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'nodes', label: '节点管理', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'skills', label: '技能库', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'credentials', label: '凭证管理', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'models', label: '模型管理', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'ai', label: 'AI Agent', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'handbook', label: '调用手册', group: 'tools', group_label: '资源与工具', audience: 'all', locked: false },
  { key: 'dashboard', label: '指标大盘', group: 'system', group_label: '系统管理', audience: 'admin', locked: true },
  { key: 'releases', label: '发布管理', group: 'system', group_label: '系统管理', audience: 'admin', locked: true },
  { key: 'users', label: '用户管理', group: 'system', group_label: '系统管理', audience: 'admin', locked: true },
  { key: 'settings', label: '平台设置', group: 'system', group_label: '系统管理', audience: 'admin', locked: true },
]

export const WORKBENCH_KEYS = FALLBACK_MENU_ITEMS.filter((item) => item.group === 'workbench').map((item) => item.key)
export const TOOL_MENU_KEYS = FALLBACK_MENU_ITEMS.filter((item) => item.group === 'tools').map((item) => item.key)
export const ADMIN_MENU_KEYS = FALLBACK_MENU_ITEMS.filter((item) => item.group === 'system').map((item) => item.key)

/** 当前用户看不见这项时，侧栏和路由都不要放行。 */
export function canSeeMenu(catalog: MenuCatalog | undefined, key: string, isAdmin: boolean): boolean {
  if (isAdmin) return true
  if (catalog?.visible) return catalog.visible.includes(key)
  const item = FALLBACK_MENU_ITEMS.find((row) => row.key === key)
  return !item || item.audience === 'all'
}
