import { useMemo, useState } from 'react'
import { Layout, Menu, Avatar, Dropdown, Drawer, Button, Space, Tag } from 'antd'
import {
  DeploymentUnitOutlined,
  LogoutOutlined,
  UserOutlined,
  KeyOutlined,
  MenuOutlined,
} from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/auth'
import { get } from '@/api/client'
import { FALLBACK_MENU_ITEMS, MENU_ICONS, canSeeMenu, type MenuCatalog } from '@/menus'
import ApiTokenModal from '@/components/ApiTokenModal'
import LanguageSwitch from '@/components/LanguageSwitch'
import { useT } from '@/i18n'
import {
  resolveDisplayName,
  resolveUserDisplayName,
  resolveNoticeColor,
  HEADER_NOTICE_COLORS,
  usePlatformBranding,
} from '@/hooks/usePlatformBranding'
import { useIsMobile } from '@/hooks/useIsMobile'
import { NoticeBell } from '@/modules/notify'
import SystemHealthBar from '@/components/SystemHealthBar'
import AdminEmailGate from '@/components/AdminEmailGate'

const { Sider, Header, Content } = Layout
const SIDER_COLLAPSE_KEY = 'rp-sider-collapsed'

export default function MainLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()
  const { data: branding } = usePlatformBranding()
  const t = useT()
  const displayName = resolveDisplayName(branding, t)
  const headerImageUrl = (branding?.header_image_url || '').trim()
  const headerNoticeText = (branding?.header_notice_text || '').trim()
  const headerNoticeColor = resolveNoticeColor(branding?.header_notice_color)
  const headerNoticeTheme = HEADER_NOTICE_COLORS[headerNoticeColor]
  const isMobile = useIsMobile()
  /** 手机上侧栏改成抽屉，点汉堡打开。 */
  const [menuOpen, setMenuOpen] = useState(false)
  const [tokenModalOpen, setTokenModalOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDER_COLLAPSE_KEY) === '1',
  )

  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev
      localStorage.setItem(SIDER_COLLAPSE_KEY, next ? '1' : '0')
      return next
    })
  }

  const selectedKey = useMemo(() => {
    const path = location.pathname
    if (path.startsWith('/projects')) return 'projects'
    if (path.startsWith('/pipeline')) return 'projects'
    // 执行历史 / 执行明细属于流水线上下文，高亮「项目与流水线」
    if (path.startsWith('/executions')) return 'projects'
    if (path.startsWith('/releases')) return 'releases'
    if (path.startsWith('/approvals')) return 'approvals'
    if (path.startsWith('/agents')) return 'agents'
    if (path.startsWith('/nodes')) return 'nodes'
    if (path.startsWith('/pm')) return 'pm'
    if (path.startsWith('/deploy-requests')) return 'deploy-requests'
    if (path.startsWith('/artifacts')) return 'artifacts'
    if (path.startsWith('/skills') || path.startsWith('/store')) return 'skills'
    if (path.startsWith('/credentials')) return 'credentials'
    if (path.startsWith('/ai')) return 'ai'
    if (path.startsWith('/handbook')) return 'handbook'
    if (path.startsWith('/models')) return 'models'
    if (path.startsWith('/settings')) return 'settings'
    if (path.startsWith('/users')) return 'users'
    if (path.startsWith('/permissions')) return 'permissions'
    if (path.startsWith('/notifications')) return 'notifications'
    // 兜底是「指标大盘」，所以漏掉哪条路由，症状就是站在那个页面上却高亮大盘
    return 'dashboard'
  }, [location.pathname])

  const { data: menuCatalog } = useQuery({
    queryKey: ['menus'],
    queryFn: () => get<MenuCatalog>('/menus'),
    retry: false,
  })
  const isAdmin = !!user?.is_admin
  const menuItems = useMemo(() => {
    const rows = menuCatalog?.items?.length ? menuCatalog.items : FALLBACK_MENU_ITEMS
    return rows
      .filter((item) => canSeeMenu(menuCatalog, item.key, isAdmin))
      .map((item) => ({
        key: item.key,
        icon: MENU_ICONS[item.key],
        label: t(`menu.${item.key}`),
      }))
  }, [menuCatalog, isAdmin, t])

  /**
   * 点菜单项后跳转。手机上再关上抽屉，否则会挡住刚打开的审批页。
   */
  const goMenu = (key: string) => {
    if (key === 'dashboard') navigate('/')
    else if (key === 'projects') navigate('/projects')
    else navigate(`/${key}`)
    if (isMobile) setMenuOpen(false)
  }

  const sideMenu = (
    <Menu
      theme="dark"
      mode="inline"
      selectedKeys={[selectedKey]}
      items={menuItems}
      style={{ borderInlineEnd: 'none', overflow: 'auto', height: 'calc(100vh - 56px)' }}
      onClick={({ key }) => goMenu(key)}
    />
  )

  return (
    <Layout className={isMobile ? 'app-shell is-mobile' : 'app-shell'} style={{ height: '100dvh' }}>
      {!isMobile && (
        <Sider
          className="app-sider"
          width={220}
          collapsedWidth={64}
          theme="dark"
          collapsed={collapsed}
          trigger={null}
          style={{ height: '100vh' }}
        >
          <div className="sider-brand">
            <button
              type="button"
              className="sider-brand-toggle"
              title={collapsed ? t('layout.expandMenu') : t('layout.collapseMenu')}
              aria-label={collapsed ? t('layout.expandMenu') : t('layout.collapseMenu')}
              onClick={toggleCollapsed}
            >
              <DeploymentUnitOutlined />
            </button>
            {!collapsed && <span>{displayName}</span>}
          </div>
          {sideMenu}
        </Sider>
      )}

      {isMobile && (
        <Drawer
          placement="left"
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          width={220}
          closable={false}
          styles={{ body: { padding: 0, background: '#001529' }, header: { display: 'none' } }}
        >
          <div className="sider-brand">
            <span style={{ color: '#fff' }}>{displayName}</span>
          </div>
          {sideMenu}
        </Drawer>
      )}

      <Layout>
        <Header className="rp-header">
          {isMobile && (
            <Button
              type="text"
              className="rp-header-menu"
              icon={<MenuOutlined />}
              aria-label={t('layout.openMenu')}
              onClick={() => setMenuOpen(true)}
            />
          )}
          <div className="header-brand-row">
            {headerImageUrl ? (
              <img
                className="header-brand-image"
                src={headerImageUrl}
                alt=""
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                }}
              />
            ) : null}
            {headerNoticeText ? (
              <div
                className="header-notice-banner"
                style={{ background: headerNoticeTheme.bg, color: headerNoticeTheme.fg }}
                title={headerNoticeText}
              >
                {headerNoticeText}
              </div>
            ) : null}
          </div>
          <Space size={isMobile ? 8 : 16}>
            {!isMobile && <SystemHealthBar />}
            <NoticeBell />
            <LanguageSwitch />
            <Dropdown
            menu={{
              items: [
                {
                  key: 'api-token',
                  icon: <KeyOutlined />,
                  label: t('layout.apiToken'),
                  onClick: () => setTokenModalOpen(true),
                },
                { type: 'divider' },
                {
                  key: 'logout',
                  icon: <LogoutOutlined />,
                  label: t('layout.logout'),
                  onClick: () => {
                    logout()
                    navigate('/login')
                  },
                },
              ],
            }}
          >
            <Space style={{ cursor: 'pointer' }}>
              <Avatar size="small" icon={<UserOutlined />} />
              <span className="header-user-name">{resolveUserDisplayName(user, t)}</span>
              {user?.is_admin && (
                <Tag className="header-user-tag" color="gold">
                  {t('layout.admin')}
                </Tag>
              )}
            </Space>
            </Dropdown>
          </Space>
        </Header>

        <Content className="rp-content">
          <Outlet />
        </Content>
      </Layout>

      <ApiTokenModal open={tokenModalOpen} onClose={() => setTokenModalOpen(false)} />
      <AdminEmailGate />
    </Layout>
  )
}
