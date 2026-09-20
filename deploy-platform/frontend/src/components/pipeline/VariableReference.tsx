import { useMemo, useState } from 'react'
import { Alert, Modal, Tabs, Empty, Button, Tooltip, Input } from 'antd'
import { CopyOutlined, SearchOutlined } from '@ant-design/icons'
import { copyText } from '@/utils/clipboard'
import type { Variable } from '@/api/types'
import { useT } from '@/i18n'

interface VariableReferenceProps {
  open: boolean
  onClose: () => void
  /** 流水线级全局变量（自定义变量） */
  variables?: Variable[]
}

/**
 * 蓝盾风格的「引用变量」弹窗。
 * - 系统变量（BK_CI_*）：只列后端 pipeline/variables.py 真正会注入的，
 *   列了填不出来的变量，用户拿到的是空串，比不支持更难排查
 * - 自定义变量：流水线级全局变量
 * - 支持搜索过滤，每行右侧「复制」按钮把占位符写入剪贴板
 */
export default function VariableReference({ open, onClose, variables = [] }: VariableReferenceProps) {
  const t = useT()
  const [keyword, setKeyword] = useState('')

  const filter = (list: { name: string; desc: string; placeholder: string }[]) => {
    if (!keyword) return list
    const kw = keyword.toLowerCase()
    return list.filter(
      (v) => v.name.toLowerCase().includes(kw) || v.desc.toLowerCase().includes(kw)
    )
  }

  const customVars = variables.map((v) => ({
    name: v.name,
    desc: v.description || '—',
    placeholder: `\${{${v.name}}}`,
  }))

  const copy = (text: string, label: string) => {
    void copyText(text, t('pipe.copiedVar', { label }))
  }
  const sys = systemVars(t)

  return (
    <Modal
      title={t("pipe.refVar")}
      open={open}
      onCancel={onClose}
      footer={null}
      width={720}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("pipe.placeholderHint")}
        description={t("pipe.placeholderShell")}
      />
      <Input
        placeholder={t("pipe.searchVar")}
        prefix={<SearchOutlined />}
        value={keyword}
        onChange={(e) => setKeyword(e.target.value)}
        style={{ marginBottom: 12 }}
        allowClear
      />
      <Tabs
        items={[
          {
            key: 'system',
            label: t('pipe.sysVars', { n: sys.length }),
            children: <VariableList items={filter(sys)} onCopy={copy} />,
          },
          {
            key: 'custom',
            label: t('pipe.customVars', { n: customVars.length }),
            children:
              customVars.length === 0 ? (
                <Empty description={t("pipe.noCustomVars")} />
              ) : (
                <VariableList items={filter(customVars)} onCopy={copy} />
              ),
          },
        ]}
      />
    </Modal>
  )
}

// ============================================================
// 系统变量：与后端 app/modules/pipeline/variables.py 的 system_variables() 一一对应。
// 改后端记得同步这里，两边对不上用户就会拿到没被替换的占位符。
// ============================================================
/** 系统变量列表：码与后端一致，说明文字走界面语言。 */
function systemVars(t: (k: string) => string): { name: string; desc: string; placeholder: string }[] {
  return [
    { name: 'BK_CI_BUILD_NUM', desc: t('pipe.varBuildNum'), placeholder: '${{BK_CI_BUILD_NUM}}' },
    { name: 'BK_CI_BUILD_NO', desc: t('pipe.varBuildNumAlias'), placeholder: '${{BK_CI_BUILD_NO}}' },
    { name: 'BK_CI_BUILD_ID', desc: t('pipe.varReleaseId'), placeholder: '${{BK_CI_BUILD_ID}}' },
    { name: 'BK_CI_BUILD_START_TIME', desc: t('pipe.varStartTime'), placeholder: '${{BK_CI_BUILD_START_TIME}}' },
    { name: 'BK_CI_VERSION', desc: t('pipe.varVersion'), placeholder: '${{BK_CI_VERSION}}' },
    { name: 'BK_CI_PIPELINE_ID', desc: t('pipe.varPipelineId'), placeholder: '${{BK_CI_PIPELINE_ID}}' },
    { name: 'BK_CI_PIPELINE_NAME', desc: t('pipe.varPipelineName'), placeholder: '${{BK_CI_PIPELINE_NAME}}' },
    { name: 'BK_CI_PIPELINE_VERSION', desc: t('pipe.varPipelineVersion'), placeholder: '${{BK_CI_PIPELINE_VERSION}}' },
    { name: 'BK_CI_PROJECT_NAME', desc: t('pipe.varProject'), placeholder: '${{BK_CI_PROJECT_NAME}}' },
    { name: 'BK_CI_PROJECT_NAME_CN', desc: t('pipe.varProjectAlias'), placeholder: '${{BK_CI_PROJECT_NAME_CN}}' },
    { name: 'BK_CI_START_TYPE', desc: t('pipe.varTrigger'), placeholder: '${{BK_CI_START_TYPE}}' },
    { name: 'BK_CI_START_USER_ID', desc: t('pipe.varUserId'), placeholder: '${{BK_CI_START_USER_ID}}' },
    { name: 'BK_CI_START_USER_NAME', desc: t('pipe.varUser'), placeholder: '${{BK_CI_START_USER_NAME}}' },
    { name: 'BK_CI_GIT_REPO_HEAD_COMMIT_ID', desc: t('pipe.varCommit'), placeholder: '${{BK_CI_GIT_REPO_HEAD_COMMIT_ID}}' },
  ]
}

function VariableList({
  items,
  onCopy,
}: {
  items: { name: string; desc: string; placeholder: string }[]
  onCopy: (text: string, label: string) => void
}) {
  const t = useT()

  if (items.length === 0) {
    return <Empty description={t('pipe.noVarMatch')} />
  }
  return (
    <div style={{ maxHeight: 480, overflow: 'auto' }}>
      {items.map((it) => (
        <div
          key={it.name}
          style={{
            display: 'flex',
            alignItems: 'center',
            padding: '8px 12px',
            borderBottom: '1px solid #f5f5f5',
            gap: 12,
          }}
        >
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 2 }}>{it.name}</div>
            <div style={{ color: '#999', fontSize: 12 }}>{it.desc}</div>
          </div>
          <Tooltip title={t("pipe.copyPlaceholder")}>
            <Button
              type="text"
              icon={<CopyOutlined />}
              onClick={() => onCopy(it.placeholder, it.name)}
            />
          </Tooltip>
        </div>
      ))}
    </div>
  )
}
