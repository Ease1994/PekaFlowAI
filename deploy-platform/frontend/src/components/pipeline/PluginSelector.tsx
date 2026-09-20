import { useEffect, useMemo, useState } from 'react'
import { Modal, Input, Menu, Button, Tag, Empty } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import type { Plugin } from '@/api/types'
import { useT } from '@/i18n'

interface PluginSelectorProps {
  open: boolean
  onClose: () => void
  onSelect: (plugin: Plugin) => void
}

/** 插件分类名随界面语言，未知分类原样显示码。 */
function categoryLabel(t: (k: string) => string, c: string): string {
  const keys: Record<string, string> = {
    source: 'pipe.catSource',
    build: 'pipe.catBuild',
    deploy: 'pipe.catDeploy',
    notify: 'pipe.catNotify',
    trigger: 'pipe.catTrigger',
    exec: 'pipe.catCommand',
    artifact: 'pipe.catArtifact',
    pipeline: 'pipe.catPipeline',
  }
  return keys[c] ? t(keys[c]) : c
}

/**
 * 插件选择器（对应蓝盾"请选择一个插件"弹窗）
 * - 左侧分类导航
 * - 右侧插件列表：图标 + 名称 + 描述 + 评分
 * - 顶部搜索 + 引用变量
 */
export default function PluginSelector({ open, onClose, onSelect }: PluginSelectorProps) {
  const t = useT()
  const [keyword, setKeyword] = useState('')
  const [category, setCategory] = useState<string>('source')

  const { data: plugins = [] } = useQuery({
    queryKey: ['plugins', 'installed'],
    queryFn: () => get<Plugin[]>('/store/plugins', { installed: true }),
    enabled: open,
  })

  const categories = useMemo(() => {
    return Array.from(new Set(plugins.map((p) => p.category)))
  }, [plugins])

  // 关键词搜索跨分类：在「源代码」里搜 shell 也该搜得到，否则等于搜不到
  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return plugins.filter((p) => p.category === category)
    return plugins.filter(
      (p) =>
        p.name.toLowerCase().includes(kw) ||
        p.display_name.toLowerCase().includes(kw) ||
        (p.description || '').toLowerCase().includes(kw),
    )
  }, [plugins, category, keyword])

  // 分类是动态的，默认的 source 不一定存在
  useEffect(() => {
    if (categories.length > 0 && !categories.includes(category)) setCategory(categories[0])
  }, [categories, category])

  return (
    <Modal
      title={t("pipe.pickPlugin")}
      open={open}
      onCancel={onClose}
      footer={null}
      width={840}
      destroyOnClose
    >
      <div style={{ display: 'flex', gap: 16, height: 480 }}>
        {/* 左侧分类 */}
        <div style={{ width: 140, borderRight: '1px solid #f0f0f0' }}>
          <Menu
            mode="inline"
            selectedKeys={[category]}
            style={{ borderRight: 0 }}
            items={categories.map((c) => ({
              key: c,
              label: categoryLabel(t, c),
            }))}
            onClick={({ key }) => setCategory(key)}
          />
        </div>

        {/* 右侧列表 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
            <Input
              placeholder={t("pipe.searchPlugin")}
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              allowClear
              style={{ width: 240 }}
            />
            {keyword.trim() && (
              <span style={{ color: '#999', fontSize: 12 }}>
                {t('pipe.matchCount', { n: filtered.length })}
              </span>
            )}
          </div>

          <div style={{ flex: 1, overflow: 'auto' }}>
            {filtered.length === 0 ? (
              <Empty description={t("pipe.noPluginMatch")} />
            ) : (
              filtered.map((p) => (
                <div
                  key={p.name}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    padding: '12px 8px',
                    borderBottom: '1px solid #f5f5f5',
                    gap: 12,
                  }}
                >
                  <div
                    style={{
                      width: 40,
                      height: 40,
                      borderRadius: 8,
                      background: '#e6f4ff',
                      color: '#1677ff',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 20,
                      flexShrink: 0,
                    }}
                  >
                    {getCategoryEmoji(p.category)}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, marginBottom: 2 }}>
                      {p.display_name} <span style={{ color: '#999', fontWeight: 400 }}>· {p.name}</span>
                    </div>
                    <div style={{ color: '#666', fontSize: 12 }}>{p.description || t('pipe.noDesc')}</div>
                    <div style={{ marginTop: 4 }}>
                      <Tag>{categoryLabel(t, p.category)}</Tag>
                      {p.version ? <Tag color="blue">v{p.version}</Tag> : null}
                    </div>
                  </div>
                  <Button
                    type="primary"
                    onClick={() => {
                      onSelect(p)
                      onClose()
                    }}
                  >
                    {t('pipe.choose')}
                  </Button>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </Modal>
  )
}

function getCategoryEmoji(category: string): string {
  const map: Record<string, string> = {
    source: '📦',
    build: '☕',
    deploy: '☸️',
    notify: '💬',
    trigger: '▶️',
    exec: '💻',
    artifact: '🗂️',
    pipeline: '🔁',
  }
  return map[category] || '🔧'
}
