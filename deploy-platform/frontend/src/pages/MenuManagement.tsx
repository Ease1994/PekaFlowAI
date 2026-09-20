/**
 * 菜单管理：工作台和系统管理锁定；资源与工具按用户取消，默认每人可见。
 * 业务数据权限仍走左侧用户授权。
 */
import { useMemo, useState } from 'react'
import { Button, Select, Space, Switch, Tag, Typography, message } from 'antd'
import DataTable from '@/components/DataTable'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, put } from '@/api/client'
import { MENU_ICONS, type MenuCatalog, type MenuNode } from '@/menus'
import { useT } from '@/i18n'

interface GroupedMenus {
  group: string
  group_label: string
  items: MenuNode[]
}

/** GET /users 的精简字段，只要能选人。 */
interface MenuUser {
  id: number
  username: string
  display_name: string
  is_admin: boolean
}

/** 某个用户被取消 / 仍保留的资源菜单。 */
interface UserMenuAssignment {
  user_id: number
  is_admin: boolean
  denied: string[]
  granted: string[]
}

export default function MenuManagement() {
  const qc = useQueryClient()
  const t = useT()
  /** 当前正在配置的普通用户。管理员不进列表，他们始终看见全部菜单。 */
  const [userId, setUserId] = useState<number>()
  /** 未保存的取消列表；null 表示跟接口返回一致。 */
  const [draftDenied, setDraftDenied] = useState<string[] | null>(null)

  const { data: catalog, isFetching } = useQuery({
    queryKey: ['menus'],
    queryFn: () => get<MenuCatalog>('/menus'),
  })
  const items = catalog?.items || []

  const { data: users = [] } = useQuery({
    queryKey: ['users'],
    queryFn: () => get<MenuUser[]>('/users'),
  })
  const staffUsers = useMemo(() => users.filter((u) => !u.is_admin), [users])

  const { data: assignment, isFetching: assignmentLoading } = useQuery({
    queryKey: ['menu-assignment', userId],
    queryFn: () => get<UserMenuAssignment>(`/menus/users/${userId}`),
    enabled: !!userId,
  })

  const denied = draftDenied ?? assignment?.denied ?? []

  const groups = useMemo(() => {
    const order: GroupedMenus[] = []
    const index = new Map<string, GroupedMenus>()
    for (const item of items) {
      let group = index.get(item.group)
      if (!group) {
        group = { group: item.group, group_label: item.group_label, items: [] }
        index.set(item.group, group)
        order.push(group)
      }
      group.items.push(item)
    }
    return order
  }, [items])

  const savedDenied = assignment?.denied || []
  const dirty = useMemo(() => {
    if (!userId || draftDenied === null) return false
    const a = [...draftDenied].sort()
    const b = [...savedDenied].sort()
    if (a.length !== b.length) return true
    return a.some((key, i) => key !== b[i])
  }, [userId, draftDenied, savedDenied])

  const saveMut = useMutation({
    mutationFn: () => put<UserMenuAssignment>(`/menus/users/${userId}`, { denied }),
    onSuccess: (next) => {
      qc.setQueryData(['menu-assignment', userId], next)
      setDraftDenied(null)
      message.success(t('menusPage.saved'))
    },
  })

  /** 勾上 = 该用户可见；去掉 = 写入取消列表。 */
  const toggleGranted = (key: string, granted: boolean) => {
    setDraftDenied((prev) => {
      const current = new Set(prev ?? assignment?.denied ?? [])
      if (granted) current.delete(key)
      else current.add(key)
      return [...current]
    })
  }

  const pickUser = (id: number) => {
    setUserId(id)
    setDraftDenied(null)
  }

  const toolsEditable = !!userId && !assignment?.is_admin

  return (
    <div>
      <div style={{ marginBottom: 12, color: '#666', fontSize: 13, lineHeight: 1.7 }}>
        {t('menusPage.hint')}
      </div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          showSearch
          style={{ width: 280 }}
          placeholder={t('menusPage.pickUser')}
          optionFilterProp="label"
          value={userId}
          onChange={pickUser}
          options={staffUsers.map((u) => ({
            value: u.id,
            label: `${u.display_name || u.username} (${u.username})`,
          }))}
        />
        <Button type="primary" disabled={!dirty} loading={saveMut.isPending} onClick={() => saveMut.mutate()}>
          {t('common.save')}
        </Button>
        <Button disabled={!dirty || saveMut.isPending} onClick={() => setDraftDenied(null)}>
          {t('common.cancel')}
        </Button>
      </Space>
      {!userId ? (
        <div style={{ marginBottom: 12, color: '#999', fontSize: 13 }}>{t('menusPage.emptyPickUser')}</div>
      ) : null}
      {groups.map((group) => (
        <div key={group.group} style={{ marginBottom: 20 }}>
          <Typography.Title level={5} style={{ marginBottom: 8 }}>
            {t(`menu.${group.group}`)}
          </Typography.Title>
          <DataTable
            chromeKey={`menus-${group.group}`}
            rowKey="key"
            size="small"
            pagination={false}
            loading={isFetching && !items.length}
            dataSource={group.items}
            columns={[
              {
                title: t('menusPage.colMenu'),
                dataIndex: 'label',
                render: (_label: string, row: MenuNode) => (
                  <Space>
                    {MENU_ICONS[row.key]}
                    <span>{t(`menu.${row.key}`)}</span>
                  </Space>
                ),
              },
              {
                title: t('menusPage.audience'),
                width: 360,
                render: (_: unknown, row: MenuNode) =>
                  row.locked ? (
                    <Tag color={row.audience === 'admin' ? 'gold' : 'blue'}>
                      {row.audience === 'admin' ? t('menusPage.adminOnly') : t('menusPage.allUsers')}
                    </Tag>
                  ) : (
                    <Space>
                      <Switch
                        checked={!denied.includes(row.key)}
                        disabled={!toolsEditable || assignmentLoading}
                        onChange={(granted) => toggleGranted(row.key, granted)}
                      />
                      <span>{t('menusPage.granted')}</span>
                    </Space>
                  ),
              },
            ]}
          />
        </div>
      ))}
    </div>
  )
}
