import { useState, type CSSProperties } from 'react'
import { Alert, Button, Empty, Input, Select, Space, Switch, Tooltip, Typography } from 'antd'
import { CopyOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import type { Variable } from '@/api/types'
import { copyText } from '@/utils/clipboard'
import { t, useT } from '@/i18n'

const { Text } = Typography

/** 变量控件类型。执行弹窗按类型渲染输入框 / 下拉 / 多行文本。 */
/** 变量类型下拉的显示名，随界面语言。 */
function variableTypes(tt: (k: string) => string) {
  return [
    { value: 'text', label: tt('pipe.typeText') },
    { value: 'textarea', label: tt('pipe.typeTextarea') },
    { value: 'number', label: tt('pipe.typeNumber') },
    { value: 'boolean', label: tt('pipe.typeBoolean') },
    { value: 'select', label: tt('pipe.typeSelect') },
    { value: 'git_ref', label: tt('pipe.typeGitBranch') },
    { value: 'code_lib', label: tt('pipe.typeRepo') },
    { value: 'artifact', label: tt('pipe.typeArtifact') },
    { value: 'pool', label: tt('pipe.typeAgentPool') },
  ]
}

/** 步骤里引用时的占位符，例如 ${{VERSION}} */
function placeholderOf(name: string): string {
  return `\${{${name || t('pipe.varNamePh')}}}`
}

/** 名称只能当占位符用，必须是标识符。 */
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/

/**
 * 给新变量起一个还不占用的 paramN。
 */
function nextParamName(variables: Variable[]): string {
  const used = new Set(variables.map((v) => v.name))
  for (let i = 1; i < 100; i++) {
    const name = `param${i}`
    if (!used.has(name)) return name
  }
  return `param${variables.length + 1}`
}

/**
 * 名称是否能保存：空、非法字符、和别的变量撞名都不行。
 */
function nameIssue(name: string, idx: number, variables: Variable[]): string {
  const trimmed = name.trim()
  if (!trimmed) return t('pipe.fillName')
  if (!NAME_RE.test(trimmed)) return t('pipe.nameAlpha')
  if (variables.some((v, i) => i !== idx && v.name === trimmed)) return t('pipe.nameExists')
  return ''
}

/**
 * 流水线变量编辑器。
 *
 * 一行里填类型、名称、默认值、是否执行时填写；说明和下拉选项叠在下面。
 * 卡片分组，避免原来那种整屏拉宽、每项占四行的排法。
 */
export function VariableEditor({
  variables,
  onChange,
}: {
  variables: Variable[]
  onChange: (next: Variable[]) => void
}) {
  const tt = useT()
  const update = (idx: number, patch: Partial<Variable>) => {
    onChange(variables.map((v, i) => (i === idx ? { ...v, ...patch } : v)))
  }
  const remove = (idx: number) => {
    onChange(variables.filter((_, i) => i !== idx))
  }
  const add = () => {
    onChange([
      ...variables,
      {
        name: nextParamName(variables),
        type: 'text',
        default_value: '',
        description: '',
        show_on_execution: true,
      },
    ])
  }

  return (
    <div style={{ maxWidth: 880 }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={tt("pipe.varHint")}
        description={
          <span>{tt('pipe.varHintExtra')}</span>
        }
      />

      {variables.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={tt("pipe.noVars")}
          style={{ padding: '32px 0' }}
        />
      ) : (
        <div>
          <div style={HEADER}>
            <span style={{ width: COL.type }}>{tt("pipe.colType")}</span>
            <span style={{ width: COL.name }}>{tt("common.name")}</span>
            <span style={{ flex: 1 }}>{tt("pipe.defaultValue")}</span>
            <span style={{ width: COL.runtime, textAlign: 'center' }}>{tt("pipe.runtimeFill")}</span>
            <span style={{ width: COL.action }} />
          </div>
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {variables.map((v, idx) => (
              <VariableCard
                key={idx}
                variable={v}
                issue={nameIssue(v.name, idx, variables)}
                onChange={(patch) => update(idx, patch)}
                onRemove={() => remove(idx)}
              />
            ))}
          </Space>
        </div>
      )}

      <Button type="dashed" icon={<PlusOutlined />} onClick={add} style={{ marginTop: 12 }}>
        添加变量
      </Button>
    </div>
  )
}

const COL = {
  type: 128,
  name: 188,
  runtime: 88,
  action: 36,
}

const HEADER: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  gap: 8,
  padding: '0 14px 8px',
  color: '#8c8c8c',
  fontSize: 12,
}

/**
 * 单条变量。主行填会反复改的字段，说明和下拉选项按需出现，不占默认视线。
 */
function VariableCard({
  variable,
  issue,
  onChange,
  onRemove,
}: {
  variable: Variable
  issue: string
  onChange: (patch: Partial<Variable>) => void
  onRemove: () => void
}) {
  const tt = useT()
  const [hovered, setHovered] = useState(false)
  const refText = placeholderOf(variable.name)

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        border: issue ? '1px solid #ffccc7' : '1px solid #e8edf3',
        borderRadius: 8,
        padding: '12px 14px 10px',
        background: hovered ? '#fafcff' : '#fff',
        boxShadow: hovered ? '0 4px 12px rgba(15, 23, 42, 0.06)' : '0 1px 2px rgba(15, 23, 42, 0.04)',
        transition: 'box-shadow 0.15s ease, background 0.15s ease',
      }}
    >
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: 8 }}>
        <Select
          value={variable.type}
          onChange={(t) => onChange({ type: t })}
          options={variableTypes(tt)}
          style={{ width: COL.type, flexShrink: 0 }}
        />
        <div style={{ width: COL.name, flexShrink: 0 }}>
          <Input
            value={variable.name}
            onChange={(e) => onChange({ name: e.target.value.trim() })}
            placeholder="VERSION"
            status={issue ? 'error' : undefined}
          />
          {issue ? <div style={{ color: '#ff4d4f', fontSize: 12, marginTop: 4 }}>{issue}</div> : null}
        </div>
        <div style={{ flex: 1, minWidth: 140 }}>
          <DefaultValueField variable={variable} onChange={onChange} />
        </div>
        <div style={{ width: COL.runtime, flexShrink: 0, display: 'flex', justifyContent: 'center', paddingTop: 5 }}>
          <Tooltip title={variable.show_on_execution ? tt('pipe.runtimeYes') : tt('pipe.runtimeNo')}>
            <Switch
              checked={variable.show_on_execution}
              onChange={(checked) => onChange({ show_on_execution: checked })}
            />
          </Tooltip>
        </div>
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          onClick={onRemove}
          style={{ width: COL.action, flexShrink: 0 }}
        />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
        <Tooltip title={tt("pipe.copyToStep")}>
          <Button
            size="small"
            type="text"
            icon={<CopyOutlined />}
            onClick={() => void copyText(refText, tt('pipe.copiedRef', { ref: refText }))}
            style={{ color: '#1677ff', paddingInline: 6 }}
          >
            <Text code style={{ fontSize: 12 }}>
              {refText}
            </Text>
          </Button>
        </Tooltip>
        <Input
          value={variable.description}
          onChange={(e) => onChange({ description: e.target.value })}
          placeholder={tt("pipe.descOptional")}
          variant="borderless"
          style={{ flex: 1, background: '#f7f8fa', borderRadius: 6 }}
        />
      </div>

      {variable.type === 'select' ? (
        <Select
          mode="tags"
          value={variable.options || []}
          onChange={(opts) => onChange({ options: opts })}
          tokenSeparators={[',']}
          placeholder={tt("pipe.selectOptions")}
          style={{ width: '100%', marginTop: 8 }}
        />
      ) : null}
    </div>
  )
}

/**
 * 按类型换默认值控件。是/否用下拉，多行用自适应文本域，其余一行输入。
 */
function DefaultValueField({
  variable,
  onChange,
}: {
  variable: Variable
  onChange: (patch: Partial<Variable>) => void
}) {
  const tt = useT()
  if (variable.type === 'boolean') {
    return (
      <Select
        allowClear
        value={variable.default_value || undefined}
        onChange={(val) => onChange({ default_value: val || '' })}
        placeholder={tt("pipe.defaultLabel")}
        options={[
          { value: 'true', label: tt('pipe.yes') },
          { value: 'false', label: tt('pipe.no') },
        ]}
        style={{ width: '100%' }}
      />
    )
  }
  if (variable.type === 'textarea') {
    return (
      <Input.TextArea
        value={variable.default_value}
        onChange={(e) => onChange({ default_value: e.target.value })}
        placeholder={tt("pipe.defaultValue")}
        autoSize={{ minRows: 1, maxRows: 4 }}
      />
    )
  }
  if (variable.type === 'select') {
    const options = (variable.options || []).map((o) => ({ value: o, label: o }))
    return (
      <Select
        allowClear
        value={variable.default_value || undefined}
        onChange={(val) => onChange({ default_value: val || '' })}
        placeholder={tt("pipe.defaultOption")}
        options={options}
        style={{ width: '100%' }}
      />
    )
  }
  return (
    <Input
      value={variable.default_value}
      onChange={(e) => onChange({ default_value: e.target.value })}
      placeholder={tt("pipe.defaultEmpty")}
    />
  )
}
