import { useState } from 'react'
import { Button, Modal, Radio, Space, Table, Tag, Upload, message } from 'antd'
import { ExportOutlined, ImportOutlined } from '@ant-design/icons'
import { postLong } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useT } from '@/i18n'
import { groupDisplayName } from '@/env'

/** 导入预览：重名流水线需要用户选覆盖、新建副本或跳过。 */
export interface CatalogConflict {
  key: string
  project_code: string
  project_name: string
  name: string
  group_name: string
  existing_id?: number
}

export interface CatalogPreview {
  new_projects: { code: string; name: string; pipeline_count: number; can_create: boolean }[]
  conflicts: CatalogConflict[]
  creates: CatalogConflict[]
  skipped: { reason: string; project_code?: string; name?: string }[]
  conflict_count: number
  create_count: number
}

export interface CatalogImportResult {
  imported: number
  results: { key?: string; name?: string; project_code?: string; action: string; id?: number; reason?: string }[]
}

interface CatalogTransferButtonsProps {
  /** 不传则导出当前用户可见的全部项目和流水线 */
  projectId?: number
  /** 项目页勾选的流水线；空则导出整个项目（或全部） */
  pipelineIds?: number[]
  onImported?: () => void
}

type Decision = 'overwrite' | 'copy' | 'skip'

/** 把导出的 JSON 触发浏览器下载。 */
function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

/** 文件名里的时间戳，避免连续导出互相覆盖。 */
function stamp(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`
}

/**
 * 项目列表和流水线页共用的导入导出。仅管理员可见。
 * 先预览再落地：重名流水线列出来，由用户选覆盖、新建 name_copy 或跳过。
 */
export default function CatalogTransferButtons({
  projectId,
  pipelineIds,
  onImported,
}: CatalogTransferButtonsProps) {
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  const t = useT()
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [bundle, setBundle] = useState<unknown>(null)
  const [preview, setPreview] = useState<CatalogPreview | null>(null)
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})

  const exportLabel = pipelineIds?.length
    ? t('catalog.exportSelected', { n: pipelineIds.length })
    : projectId
      ? t('catalog.exportProject')
      : t('catalog.exportAll')

  /** 按当前范围拉配置包并触发浏览器下载。 */
  const handleExport = async () => {
    setExporting(true)
    try {
      const data = await postLong(
        '/pipelines/export',
        {
          project_ids: projectId ? [projectId] : undefined,
          pipeline_ids: pipelineIds?.length ? pipelineIds : undefined,
        },
        120000,
      )
      const projects = (data as { projects?: unknown[] })?.projects || []
      if (!projects.length) {
        message.warning(t('catalog.none'))
        return
      }
      const name = pipelineIds?.length
        ? `rp-pipelines-${stamp()}.json`
        : projectId
          ? `rp-project-${stamp()}.json`
          : `rp-catalog-${stamp()}.json`
      downloadJson(name, data)
      message.success(t('catalog.downloading'))
    } finally {
      setExporting(false)
    }
  }

  /** 读 JSON → 预览冲突。重名默认选新建副本，避免误覆盖。 */
  const handleFile = async (file: File) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(await file.text())
    } catch {
      message.error(t('catalog.invalidJson'))
      return false
    }
    setImporting(true)
    try {
      const data = await postLong<CatalogPreview>(
        '/pipelines/import/preview',
        { bundle: parsed },
        120000,
      )
      const initial: Record<string, Decision> = {}
      for (const row of data.conflicts) {
        initial[row.key] = 'copy'
      }
      setBundle(parsed)
      setPreview(data)
      setDecisions(initial)
      setPreviewOpen(true)
    } finally {
      setImporting(false)
    }
    return false
  }

  /** 把表格里所有冲突项设成同一种处理方式。 */
  const applyAll = (action: Decision) => {
    if (!preview) return
    const next: Record<string, Decision> = {}
    for (const row of preview.conflicts) {
      next[row.key] = action
    }
    setDecisions(next)
  }

  /** 按用户选择落地。冲突项必须每条都有 overwrite、copy 或 skip。 */
  const handleImport = async () => {
    if (!preview || !bundle) return
    const undecided = preview.conflicts.filter((c) => !decisions[c.key])
    if (undecided.length) {
      message.warning(t('catalog.needDecision'))
      return
    }
    setImporting(true)
    try {
      const res = await postLong<CatalogImportResult>(
        '/pipelines/import',
        { bundle, decisions },
        120000,
      )
      const skipped = (res.results || []).filter((r) => r.action === 'skip')
      const failed = (res.results || []).filter((r) => r.action === 'error')
      if (res.imported) {
        message.success(t('catalog.imported', { n: res.imported }))
      }
      if (skipped.length) {
        message.info(t('catalog.skipped', { n: skipped.length }))
      }
      if (failed.length) {
        message.warning(t('catalog.failed', { n: failed.length, reason: failed.map((f) => f.reason || f.action).join('; ') }))
      }
      if (!res.imported && !skipped.length && !failed.length) {
        message.warning(t('catalog.nothing'))
      }
      setPreviewOpen(false)
      setBundle(null)
      setPreview(null)
      onImported?.()
    } finally {
      setImporting(false)
    }
  }

  const blockedProjects = (preview?.new_projects || []).filter((p) => !p.can_create)

  if (!isAdmin) return null

  return (
    <>
      <Space>
        <Button icon={<ExportOutlined />} loading={exporting} onClick={() => void handleExport()}>
          {exportLabel}
        </Button>
        <Upload accept=".json,application/json" showUploadList={false} beforeUpload={handleFile}>
          <Button icon={<ImportOutlined />} loading={importing}>
            {t('catalog.import')}
          </Button>
        </Upload>
      </Space>

      <Modal
        title={t("catalog.title")}
        open={previewOpen}
        onCancel={() => setPreviewOpen(false)}
        onOk={() => void handleImport()}
        confirmLoading={importing}
        okText={t("catalog.start")}
        width={820}
        destroyOnClose
      >
        {preview ? (
          <div>
            <p>
              {t('catalog.summaryNew', { n: preview.create_count })}
              {preview.new_projects.length
                ? t('catalog.summaryNewProjects', { names: preview.new_projects.map((p) => p.name).join(', ') })
                : ''}
              {preview.conflict_count ? t('catalog.summaryConflict', { n: preview.conflict_count }) : ''}
              {t('catalog.sentenceEnd')}
            </p>
            {blockedProjects.length ? (
              <p style={{ color: '#d4380d' }}>
                {t('catalog.blocked', { names: blockedProjects.map((bp) => `${bp.name} (${bp.code})`).join(', ') })}
              </p>
            ) : null}
            {preview.skipped.length ? (
              <p>
                {t('catalog.previewSkipped', { n: preview.skipped.length })}
                {preview.skipped[0]?.reason ? `（${preview.skipped[0].reason}）` : ''}
              </p>
            ) : null}
            {preview.conflicts.length ? (
              <>
                <Space style={{ marginBottom: 8 }} wrap>
                  <Button size="small" onClick={() => applyAll('overwrite')}>
                    {t('catalog.overwriteAll')}
                  </Button>
                  <Button size="small" onClick={() => applyAll('copy')}>
                    {t('catalog.copyAll')}
                  </Button>
                  <Button size="small" onClick={() => applyAll('skip')}>
                    {t('catalog.skipAll')}
                  </Button>
                </Space>
                <Table
                  rowKey="key"
                  size="small"
                  pagination={false}
                  dataSource={preview.conflicts}
                  columns={[
                    { title: t('catalog.colProject'), dataIndex: 'project_code', width: 120 },
                    { title: t('catalog.colPipeline'), dataIndex: 'name', ellipsis: true },
                    {
                      title: t('catalog.colEnv'),
                      dataIndex: 'group_name',
                      width: 100,
                      render: (v: string) => groupDisplayName({ name: v }) || '—',
                    },
                    {
                      title: t('catalog.colAction'),
                      width: 280,
                      render: (_: unknown, row: CatalogConflict) => (
                        <Radio.Group
                          value={decisions[row.key]}
                          onChange={(e) =>
                            setDecisions((prev) => ({ ...prev, [row.key]: e.target.value }))
                          }
                        >
                          <Radio value="overwrite">{t("catalog.overwrite")}</Radio>
                          <Radio value="copy">{t("catalog.copy")}</Radio>
                          <Radio value="skip">{t("catalog.skip")}</Radio>
                        </Radio.Group>
                      ),
                    },
                  ]}
                />
              </>
            ) : (
<Tag color="green">{t("catalog.noConflict")}</Tag>
            )}
          </div>
        ) : null}
      </Modal>
    </>
  )
}
