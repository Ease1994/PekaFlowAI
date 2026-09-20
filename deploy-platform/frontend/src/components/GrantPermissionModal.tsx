import { useEffect, useMemo, useState } from 'react'
import { Alert, Form, Input, Modal, Select, Checkbox } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import { envLabel, groupOptionLabel } from '@/env'
import { useT } from '@/i18n'

/** 授权对象：流水线三级（项目/分组/流水线）或节点两级（节点组/节点）。 */
export type GrantKind = 'pipeline' | 'node'
/** 真正写入权限表的资源类型。 */
export type GrantResourceType = 'project' | 'group' | 'pipeline' | 'node' | 'node_group'

/** 被授权用户，弹窗顶部只读展示。 */
export interface GrantUser {
  id: number
  username: string
  display_name: string
}

/** 项目下拉项。 */
interface ProjectItem {
  id: number
  name: string
  code: string
}
/** 环境分组下拉项。 */
interface GroupItem {
  id: number
  name: string
  type: string
  project_id: number
}
/** 流水线下拉项；搜索结果带 group_id，选项上能标所属分组。 */
interface PipelineItem {
  id: number
  name: string
  project_id: number
  group_id: number
}
/** 部署节点下拉项。 */
interface NodeItem {
  id: number
  name: string
  env?: string
  allow_paths?: string[]
}
/** 节点组；agent_ids 用来按组过滤节点，不必再拉全站。 */
interface NodeGroupItem {
  id: number
  name: string
  description: string
  member_count: number
  agent_ids?: number[]
}

/** 提交给 /permissions/batch 的载荷。 */
export interface GrantPayload {
  resource_type: GrantResourceType
  resource_ids: number[]
  actions: string[]
}

/** 不选分组时按名称搜，最多拿这么多条，避免一次倒出几万条。 */
const PIPELINE_SEARCH_LIMIT = 50
/** 关键字最短长度：少打几个字就能搜，不必先回忆完整名字。 */
const PIPELINE_SEARCH_MIN = 1

/**
 * 执行权会带上查看：没有查看就进不了页面，授了执行也点不到。
 */
function withRead(actions: string[]) {
  if (actions.includes('execute') && !actions.includes('read')) return ['read', ...actions]
  return actions
}

/**
 * 按当前联动选择算出真正授在哪一层。
 * 项目空着就不授；分组不选 = 整个项目；流水线不选 = 整个分组（或整个项目）。
 */
function resolvePipelineGrant(
  projectId: number | undefined,
  groupId: number | undefined,
  pipelineIds: number[],
): { resource_type: GrantResourceType; resource_ids: number[] } | null {
  if (!projectId) return null
  if (pipelineIds.length) return { resource_type: 'pipeline', resource_ids: pipelineIds }
  if (groupId) return { resource_type: 'group', resource_ids: [groupId] }
  return { resource_type: 'project', resource_ids: [projectId] }
}

/**
 * 节点组不选、机器也不选就不能授。只选组 = 整组；挑了机器就只授这几台。
 */
function resolveNodeGrant(
  nodeGroupId: number | undefined,
  nodeIds: number[],
): { resource_type: GrantResourceType; resource_ids: number[] } | null {
  if (nodeIds.length) return { resource_type: 'node', resource_ids: nodeIds }
  if (nodeGroupId) return { resource_type: 'node_group', resource_ids: [nodeGroupId] }
  return null
}

/**
 * 把已选流水线留在选项里：服务端搜索换关键字后，已勾的项不能从下拉里消失。
 */
function mergePipelineOptions(fetched: PipelineItem[], picked: PipelineItem[]) {
  const map = new Map<number, PipelineItem>()
  for (const p of fetched) map.set(p.id, p)
  for (const p of picked) if (!map.has(p.id)) map.set(p.id, p)
  return [...map.values()]
}

/**
 * 添加授权：项目 / 分组 / 流水线联动，节点组 / 节点同样。
 * 不记得完整名字时先选项目，再按关键字搜；选了分组则只列该组。
 */
export default function GrantPermissionModal({
  open,
  kind,
  user,
  projects,
  groups,
  nodes,
  nodeGroups,
  confirmLoading,
  onCancel,
  onSubmit,
}: {
  open: boolean
  kind: GrantKind
  user?: GrantUser
  projects: ProjectItem[]
  groups: GroupItem[]
  nodes: NodeItem[]
  nodeGroups: NodeGroupItem[]
  confirmLoading: boolean
  onCancel: () => void
  onSubmit: (payload: GrantPayload) => void
}) {
  const t = useT()
  /** 当前选中的项目，流水线授权的起点。 */
  const [projectId, setProjectId] = useState<number>()
  /** 当前选中的分组；空 = 整个项目。 */
  const [groupId, setGroupId] = useState<number>()
  /** 指定授权的流水线 id；空 = 授在项目或分组上。 */
  const [pipelineIds, setPipelineIds] = useState<number[]>([])
  /** 已选流水线的完整记录，换搜索词后用来补选项。 */
  const [pickedPipelines, setPickedPipelines] = useState<PipelineItem[]>([])
  /** 流水线搜索框里正在打的字。 */
  const [pipelineKeyword, setPipelineKeyword] = useState('')
  /** 防抖后的关键字，真正拿去请求接口。 */
  const [pipelineSearch, setPipelineSearch] = useState('')
  /** 当前选中的节点组；空且未挑机器则不能提交。 */
  const [nodeGroupId, setNodeGroupId] = useState<number>()
  /** 指定授权的节点；空 = 整个节点组。 */
  const [nodeIds, setNodeIds] = useState<number[]>([])
  /** 勾选的操作。 */
  const [actions, setActions] = useState<string[]>(['read', 'execute'])

  /** 项目/分组上可选的操作；授在这两层时审批权会覆盖该范围内全部流水线。 */
  const pipelineActions = [
    { value: 'read', label: t('acl.view') },
    { value: 'create', label: t('acl.create') },
    { value: 'update', label: t('acl.update') },
    { value: 'delete', label: t('acl.delete') },
    { value: 'execute', label: t('acl.execute') },
    { value: 'approve', label: t('acl.approve') },
    { value: 'approval_exempt', label: t('acl.exempt') },
  ]
  /** 单条流水线没有审批权，审批落在项目或分组上。 */
  const pipelineOnlyActions = pipelineActions.filter((a) => a.value !== 'approve')
  /** 节点只授下发文件。 */
  const nodeActions = [{ value: 'deploy', label: t('acl.pushFile') }]

  useEffect(() => {
    if (!open) return
    setProjectId(undefined)
    setGroupId(undefined)
    setPipelineIds([])
    setPickedPipelines([])
    setPipelineKeyword('')
    setPipelineSearch('')
    setNodeGroupId(undefined)
    setNodeIds([])
  }, [open])

  useEffect(() => {
    if (!open) return
    setActions(kind === 'node' ? ['deploy'] : ['read', 'execute'])
  }, [open, kind])

  useEffect(() => {
    const timer = window.setTimeout(() => setPipelineSearch(pipelineKeyword.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [pipelineKeyword])

  /** 当前项目下的分组，换项目后下拉只剩这些。 */
  const projectGroups = useMemo(
    () => groups.filter((g) => g.project_id === projectId),
    [groups, projectId],
  )
  /** 分组 id → 名称，流水线选项上标所属分组。 */
  const groupNameById = useMemo(
    () => new Map(groups.map((g) => [g.id, g.name])),
    [groups],
  )

  const searchByName = !groupId && pipelineSearch.length >= PIPELINE_SEARCH_MIN
  const { data: fetchedPipelines = [], isFetching: pipelinesLoading } = useQuery({
    queryKey: ['pipelines', 'grant', projectId, groupId, searchByName ? pipelineSearch : ''],
    queryFn: () => {
      const params: Record<string, unknown> = { project_id: projectId }
      if (groupId) {
        params.group_id = groupId
      } else {
        params.q = pipelineSearch
        params.limit = PIPELINE_SEARCH_LIMIT
      }
      return get<PipelineItem[]>('/pipelines', params)
    },
    enabled: open && kind === 'pipeline' && !!projectId && (!!groupId || searchByName),
  })

  const pipelineOptions = useMemo(
    () => mergePipelineOptions(fetchedPipelines, pickedPipelines),
    [fetchedPipelines, pickedPipelines],
  )

  const selectedNodeGroup = nodeGroups.find((g) => g.id === nodeGroupId)
  const nodeOptions = useMemo(() => {
    const allowed = selectedNodeGroup?.agent_ids
    const list = allowed?.length ? nodes.filter((n) => allowed.includes(n.id)) : nodes
    return list.map((n) => ({
      value: n.id,
      label:
        t('env.groupWithEnv', { name: n.name, env: envLabel(n.env) }) +
        (n.allow_paths?.length ? ` · ${n.allow_paths.join(', ')}` : t('grant.noAllowDir')),
    }))
  }, [nodes, selectedNodeGroup, t])

  const grant = kind === 'node'
    ? resolveNodeGrant(nodeGroupId, nodeIds)
    : resolvePipelineGrant(projectId, groupId, pipelineIds)
  const actionOptions = kind === 'node'
    ? nodeActions
    : pipelineIds.length
      ? pipelineOnlyActions
      : pipelineActions

  const selectedProject = projects.find((p) => p.id === projectId)
  const selectedGroup = groups.find((g) => g.id === groupId)
  const hint = kind === 'node'
    ? nodeIds.length
      ? t('grant.onlyNodes', { n: nodeIds.length })
      : nodeGroupId
        ? t('grant.onNodeGroup', { name: selectedNodeGroup?.name || '' })
        : t('grant.pickNodeFirst')
    : pipelineIds.length
      ? t('grant.onlyPipelines', { n: pipelineIds.length })
      : selectedGroup
        ? t('grant.onGroup', { name: selectedGroup.name })
        : selectedProject
          ? t('grant.onProject', { name: selectedProject.name })
          : t('grant.pickProjectHint')

  const canSubmit = !!user && !!grant && actions.length > 0

  /**
   * 同步已选流水线 id 与完整记录。
   * 换关键字后 fetched 列表会变，必须把旧选项留在 pickedPipelines 里。
   */
  const applyPipelineIds = (ids: number[]) => {
    const byId = new Map(pipelineOptions.map((p) => [p.id, p]))
    setPickedPipelines(ids.map((id) => byId.get(id)).filter((p): p is PipelineItem => !!p))
    setPipelineIds(ids)
  }

  return (
    <Modal
      title={kind === 'node' ? t('grant.addNode') : t('grant.addPipeline')}
      open={open}
      onCancel={onCancel}
      onOk={() => {
        if (!grant) return
        const allowed = new Set(actionOptions.map((a) => a.value))
        const picked = actions.filter((a) => allowed.has(a))
        onSubmit({
          resource_type: grant.resource_type,
          resource_ids: grant.resource_ids,
          actions: kind === 'node' ? picked : withRead(picked),
        })
      }}
      confirmLoading={confirmLoading}
      okButtonProps={{ disabled: !canSubmit }}
      okText={t('grant.confirmGrant')}
      width={560}
      styles={{ content: { maxWidth: 'calc(100vw - 24px)' } }}
      destroyOnClose
    >
      <Form layout="horizontal" labelCol={{ flex: '72px' }} wrapperCol={{ flex: 1 }} colon={false} style={{ marginTop: 8 }}>
        <Form.Item label={t('grant.user')}>
          <Input
            disabled
            value={user ? t('grant.userDisplay', { name: user.display_name, username: user.username }) : ''}
          />
        </Form.Item>
        {kind === 'pipeline' ? (
          <>
            <Form.Item label={t('acl.project')} required>
              <Select
                showSearch
                optionFilterProp="label"
                placeholder={t('grant.pickProjectNarrow')}
                value={projectId}
                onChange={(id) => {
                  setProjectId(id)
                  setGroupId(undefined)
                  setPipelineIds([])
                  setPickedPipelines([])
                  setPipelineKeyword('')
                  setPipelineSearch('')
                }}
                options={projects.map((p) => ({
                  value: p.id,
                  label: t('grant.namedCode', { name: p.name, code: p.code }),
                }))}
              />
            </Form.Item>
            <Form.Item label={t('acl.group')} extra={t('grant.groupWholeProject')}>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder={t('grant.grantWholeProject')}
                disabled={!projectId}
                value={groupId}
                onChange={(id) => {
                  setGroupId(id)
                  setPipelineIds([])
                  setPickedPipelines([])
                  setPipelineKeyword('')
                  setPipelineSearch('')
                }}
                options={projectGroups.map((g) => ({
                  value: g.id,
                  label: groupOptionLabel(g),
                }))}
              />
            </Form.Item>
            <Form.Item
              label={t('acl.pipeline')}
              extra={groupId ? t('grant.pipeWholeGroup') : t('grant.pipeWholeProjectSearch')}
            >
              <Select
                mode="multiple"
                allowClear
                showSearch
                optionFilterProp="label"
                filterOption={groupId ? undefined : false}
                maxTagCount="responsive"
                placeholder={
                  !projectId
                    ? t('grant.pickProjectFirst')
                    : groupId
                      ? t('grant.grantAllInGroup')
                      : t('grant.searchNameNoGroup')
                }
                disabled={!projectId}
                loading={pipelinesLoading}
                value={pipelineIds}
                onSearch={groupId ? undefined : setPipelineKeyword}
                onChange={applyPipelineIds}
                notFoundContent={
                  !projectId
                    ? null
                    : groupId
                      ? t('grant.noPipeInGroup')
                      : pipelineSearch.length < PIPELINE_SEARCH_MIN
                        ? t('grant.searchPipeName')
                        : t('grant.noPipeMatch')
                }
                options={pipelineOptions.map((p) => ({
                  value: p.id,
                  label: groupId
                    ? p.name
                    : t('grant.pipeInGroup', {
                        name: p.name,
                        group: groupNameById.get(p.group_id) || t('project.ungrouped'),
                      }),
                }))}
              />
            </Form.Item>
          </>
        ) : (
          <>
            <Form.Item label={t('acl.nodeGroup')} extra={t('grant.nodeWholeGroup')}>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder={t('grant.grantByNode')}
                value={nodeGroupId}
                onChange={(id) => {
                  setNodeGroupId(id)
                  setNodeIds([])
                }}
                options={nodeGroups.map((g) => ({
                  value: g.id,
                  label: t('grant.nodeGroupN', { name: g.name, n: g.member_count }) + (g.description ? ` · ${g.description}` : ''),
                }))}
                notFoundContent={t('grant.noNodeGroup')}
              />
            </Form.Item>
            <Form.Item label={t('acl.node')} extra={t('grant.nodeSearchExtra')}>
              <Select
                mode="multiple"
                allowClear
                showSearch
                optionFilterProp="label"
                maxTagCount="responsive"
                placeholder={
                  nodeGroupId ? t('grant.grantAllInNodeGroup') : t('grant.searchAllNodes')
                }
                value={nodeIds}
                onChange={setNodeIds}
                options={nodeOptions}
              />
            </Form.Item>
          </>
        )}
        <Form.Item label=" " colon={false} style={{ marginBottom: 12 }}>
          <Alert type="info" showIcon message={hint} />
        </Form.Item>
        <Form.Item label={t('grant.perms')}>
          <Checkbox.Group
            value={actions}
            onChange={(v) => setActions(kind === 'node' ? (v as string[]) : withRead(v as string[]))}
            options={actionOptions}
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
