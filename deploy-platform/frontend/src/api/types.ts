// 统一响应结构
export interface R<T = unknown> {
  code: number
  message: string
  data: T
}

// 服务端分页的统一结构（制品、发布记录等只增不减的列表都用它）
export interface Paged<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// ---- 认证 ----
export interface UserInfo {
  id: number
  username: string
  display_name: string
  email: string
  is_admin: boolean
  /** local / ldap / wecom，登录来源 */
  source?: string
  /** 是否已经扫过 Authenticator 并验证过一次 */
  totp_enrolled?: boolean
  /** 本地管理员还没留下真实邮箱，必须先配才能当找回入口 */
  must_set_email?: boolean
}

export interface LoginResponse {
  /** 正式会话 JWT。双因子进行中为空，此时只能用 pending_token */
  token: string
  user: UserInfo | null
  /** ok 已登录；totp_setup 首次扫码；totp_required 已绑定只需输码 */
  status?: string
  pending_token?: string
  otpauth_uri?: string
  qr_svg?: string
  message?: string
}

// ---- 项目 / 分组 ----
export interface Project {
  id: number
  name: string
  code: string
  description: string
  status: string
  can_create_pipeline?: boolean
  can_delete_group?: boolean
  pm_enabled?: boolean
}

export interface Group {
  id: number
  project_id: number
  name: string
  type: string
  description: string
  approval_required: boolean
  allow_self_approval: boolean
  allow_emergency_bypass: boolean
  pm_approval_required?: boolean
  change_window?: string | null
  status: string
}

// ---- 流水线 ----
export interface Pipeline {
  id: number
  project_id: number
  group_id: number
  name: string
  description: string
  yaml: string
  version: number
  /** 与 BK_CI_PIPELINE_VERSION 相同：V + 最近保存时刻 yyyyMMddHHmmss */
  version_label?: string
  status: string
  can_update?: boolean
  can_delete?: boolean
  can_execute?: boolean
  can_create?: boolean
  can_approve?: boolean
  can_exempt_approval?: boolean
  // 所属分组的审批策略（发起发布时决定是否需要审批、能否应急跳审）
  group_type?: string
  // 这条流水线最终要不要审批：环境默认值经 approval_mode 覆盖后的结果
  approval_required?: boolean
  // 环境本身的默认值，只用于在设置里显示「跟随环境 = 当前需/免审批」
  group_approval_required?: boolean
  approval_mode?: 'inherit' | 'force' | 'exempt'
  /** 编辑器默认视图：form 列表式编排，canvas React Flow 画布 */
  editor_view?: 'form' | 'canvas'
  allow_self_approval?: boolean
  allow_emergency_bypass?: boolean
  /** 是否消费发布清单（库列；列表筛选用） */
  uses_deploy_manifest?: boolean
  /** 编排里有没有「提取增量发布包」步骤，执行弹窗据此决定是否出清单框 */
  uses_pack_incremental?: boolean
  /** 步骤里写死的清单原文；仍是 ${{DEPLOY_MANIFEST}} 时为空，弹窗不预填 */
  deploy_manifest_default?: string
  // 当前用户对这条流水线的个人偏好 + 最近一次执行（列表页排序/展示用）
  starred?: boolean
  folder?: string
  creator_name?: string
  created_by?: number | null
  last_run_at?: string | null
  last_release?: {
    id: number
    build_number: number
    status: string
    trigger_by: string
    trigger_label: string
    operator_name: string
    error_summary?: string
  } | null
  created_at?: string
  updated_at?: string | null
}

// ---- 发布审批 ----
export interface ReleaseApproval {
  id: number
  release_id: number
  /** 流水线构建号，和执行历史列表上的 #N 一致 */
  build_number?: number | null
  approver_id: number
  approver: string
  requester_id: number | null
  requester: string
  status: string
  comment: string
  approved_at: string | null
  created_at: string
  pipeline_id: number | null
  pipeline_name: string
  project_name: string
  group_name: string
  group_type: string
  release_status: string
  version: string
  source_ref: string
  /** 短 SHA，审批列表「变更」列用 */
  commit_short?: string
  /** commit 标题，审批列表「变更」列用 */
  commit_title?: string
  /** release / rollback / rebuild */
  action?: string
  action_label?: string
  rollback_of_release_id?: number | null
  rollback_of_build_number?: number | null
  /** 回滚计划一句话，Git 拉不到 commit 时的兜底 */
  undo_summary?: string
  trigger_by: string
  is_self_approval: boolean
  /** 当前用户能不能点通过/驳回，和 decide 接口同一套口径 */
  can_decide?: boolean
}

// ---- 流水线编排图（核心）----
export interface GraphTrigger {
  type: string
  cron?: string | null
  label: string
}

export interface GraphStep {
  name?: string
  plugin: string
  display_name: string
  icon: string
  order: number
  with?: Record<string, unknown>
  built_in: boolean
}

export interface GraphJob {
  id: string
  name: string
  agent: string
  agent_icon: string
  steps: GraphStep[]
}

// 流水线参数（蓝盾风格：Job 级别变量池）
export interface Variable {
  name: string
  type: string  // text / number / boolean / textarea / select / git_ref / code_lib / artifact / pool
  default_value: string
  description: string
  show_on_execution: boolean
  options?: string[]
}

export interface GraphStage {
  id: string
  name: string
  order: number
  jobs: GraphJob[]
}

export interface PipelineGraph {
  pipeline_id: number | null
  pipeline_name: string
  version: number
  triggers: GraphTrigger[]
  stages: GraphStage[]
  variables?: Variable[]   // 流水线级全局变量
  /** 画布节点坐标，保存后下次打开仍按用户拖过的位置摆 */
  layout?: Record<string, { x: number; y: number }>
  /**
   * 画布上已断开、尚未拉回的顺序箭头 id。
   * 不改 Job 归属；有值时不能保存，避免和 YAML 顺序不一致。
   */
  open_cuts?: string[]
}

// ---- 发布任务 ----
export interface Release {
  id: number
  /** 构建号：按流水线各自从 1 自增，页面展示用（id 是全库自增主键） */
  build_number?: number
  pipeline_id: number
  pipeline_name?: string
  project_name?: string
  operator_name?: string
  group_id: number
  artifact_id?: number | null
  version: string
  source_ref?: string | null
  /** 检出分支，执行列表「源材料」写成 流水线@分支 → commit */
  source_branch?: string | null
  strategy: string
  status: string
  trigger_by: string
  operator_id?: number | null
  /** 失败/驳回/取消时的一句话原因，列表用；不含全量日志 */
  error_summary?: string
  created_at: string
  // 所属分组的审批策略，决定回滚 / Rebuild 是否需要过审、能否应急跳审
  group_type?: string
  approval_required?: boolean
  allow_emergency_bypass?: boolean
  /** 本次回滚撤销的是哪一次发布 */
  rollback_of_release_id?: number | null
}

// ---- 构建机 ----
export interface BuildAgent {
  id: number
  name: string
  host: string
  port: number
  os: string
  tags: string
  role?: 'builder' | 'node'  // builder=构建机，node=部署节点
  env?: string  // 环境码，须与流水线分组 type 全等；节点侧 test/dev 下发可免审批
  allow_paths?: string[]  // 节点允许被操作的根目录；管理员可在节点页修改，心跳下发给 Agent
  status: string  // DB 里的原始状态（最近一次心跳时的）
  effective_status?: string  // 后端根据 last_heartbeat 推算的真实状态（online 但超时 → offline）
  last_heartbeat?: string | null
  agent_version?: string  // Agent 自报的 jar 指纹；为空说明是不会上报版本的旧 Agent
  latest_version?: string  // 平台当前 jar 的指纹
  outdated?: boolean  // 两者不一致，即这台没升到最新
  upgrading?: boolean  // 版本落后但在线且没报错，即正在自动升级的路上
  upgrade_stalled?: boolean  // 版本落后却升不上去：报了错，或者掉线了
  upgrade_error?: string  // Agent 自报的失败原因（下载失败 / 守护进程没换上包……）
  uninstall_requested?: boolean  // 已下发卸载，等 Agent 卸完或管理员确认
  uninstalled?: boolean  // 机器上的 Agent 已卸掉
  can_delete?: boolean  // 只有卸完才允许从名单删除
  groups?: { id: number; name: string }[]  // 所属节点组，下发权限授在组上
  // Linux 节点允许启停的服务，形如 systemd:nginx。真正的边界在节点本地的
  // sudoers 白名单上，这里只是 Agent 自报的、供页面对照
  allow_services?: string[]
}

/** 节点组：下发权限授在组上，机器进出组权限自动跟着变 */
export interface NodeGroup {
  id: number
  name: string
  description: string
  agent_ids: number[]
  member_count: number
}

// ---- 插件 ----
export interface Plugin {
  id: number
  name: string
  display_name: string
  category: string
  version: string
  description: string
  config_schema?: string
  language?: string
  entrypoint?: string
  package_path?: string
  installed?: boolean
  enabled?: boolean
  status?: string
  builtin?: boolean
  /** 控制台能否下载源码包；shell-exec 这类 Agent 内置步骤为 false */
  has_package?: boolean
}

// ---- 插件草稿（AI 起草，人工审过才进仓库）----
export interface LintFinding {
  level: 'error' | 'high' | 'warn'
  code: string
  message: string
  where?: string
}

export interface LintResult {
  ok?: boolean
  error_count?: number
  high_count?: number
  warn_count?: number
  findings?: LintFinding[]
}

export interface PluginDraft {
  id: number
  name: string
  display_name: string
  category: string
  version: string
  description: string
  language: string
  entrypoint: string
  source: string
  intent: string
  status: 'pending' | 'published' | 'rejected'
  review_comment: string
  created_by?: number | null
  created_at?: string | null
  lint: LintResult
  trial_release_id?: number | null
  trial_status?: string
  // 详情接口才返回
  files?: Record<string, string>
  task_json?: string
  config_schema?: Record<string, unknown>
}

// ---- DSH 风格 Harness / Agent ----
export type HarnessKind =
  | 'pipeline-plugin'
  | 'agent-skill'
  | 'agent-tool'
  | 'model-adapter'
  | 'template'

export interface HarnessManifest {
  schema_version?: string
  kind: HarnessKind
  name: string
  version: string
  display_name?: string
  description?: string
  entrypoint?: string
  capabilities?: string[]
  dependencies?: { key: string; version: string; optional?: boolean }[]
  config_schema?: Record<string, unknown>
  metadata?: Record<string, unknown>
}

export interface HarnessComponent {
  id: number
  key: string
  kind: HarnessKind
  name: string
  version: string
  manifest: HarnessManifest
  source?: string
  source_ref?: string
  owner_user_id?: number | null
  /** 全员技能的上传人。删除权归发布者或管理员，别人不能删。 */
  publisher_id?: number | null
  status: string
  enabled: boolean
  health_status?: string
  health_message?: string
  installed_at?: string | null
  enabled_at?: string | null
  last_health_at?: string | null
}

export interface HarnessRuntime {
  status: string
  loaded_components: number
  active_runtimes: number
  /** ready 表示隔离 Runner 可用；unavailable 时第三方工具一律拒绝执行 */
  isolation: 'ready' | 'unavailable'
  isolation_error?: string
  isolation_detail?: Record<string, unknown>
  by_kind?: Record<string, number>
}

export interface HarnessTool {
  name: string
  display_name?: string
  description?: string
  category?: string
  risk?: string
  confirm?: boolean
  parameters?: Record<string, unknown>
  examples?: string[]
  source?: 'builtin' | 'package' | string
  isolation?: string
  component_id?: number | null
  version?: string
  capabilities?: string[]
}

export interface HarnessLifecycleEvent {
  id: number
  extension_id?: number | null
  extension_key: string
  action: string
  from_status?: string
  to_status?: string
  success: boolean
  message?: string
  actor_name?: string
  created_at?: string
}

export type AssistantMode = 'standard' | 'minimal' | 'plan'

export interface AssistantSession {
  id: string | number
  title: string
  mode?: AssistantMode
  status?: string
  model?: string
  adapter?: string
  /** 该会话选定的模型（llm_model.id），为空表示跟随全局默认 */
  model_id?: number | null
  updated_at?: string
  created_at?: string
}

export interface AssistantCatalogItem {
  name: string
  display_name?: string
  description?: string
  kind?: string
  enabled?: boolean
  schema?: Record<string, unknown>
}

export interface AssistantEvent {
  id?: string | number
  type: 'reasoning' | 'text' | 'tool' | 'approval' | 'status' | string
  role?: 'user' | 'assistant' | 'system'
  content?: string
  name?: string
  phase?: string
  arguments?: Record<string, unknown>
  result?: unknown
  created_at?: string
}

export interface ModelRoute {
  id: number | string
  task: string
  model_name?: string
  model_id?: number | string
  fallback_model?: string
  strategy?: string
  enabled?: boolean
}

export interface ModelAdapter {
  id: number | string
  name: string
  kind?: string
  source?: string
  version?: string
  enabled?: boolean
  status?: string
  capabilities?: string[]
}

export interface ModelObservation {
  id?: number | string
  provider?: string
  model?: string
  requests?: number
  success_rate?: number
  p95_latency_ms?: number
  tokens?: number
  cost?: number
  updated_at?: string
}

// ---- 制品 ----
export interface Artifact {
  id: number
  pipeline_id: number
  name: string
  type: string
  version: string
  git_commit: string
  created_at: string
}

// ---- 代码仓库 ----
export interface Repository {
  id: number
  project_id: number
  name: string
  alias: string  // 从 URL 自动派生的 group/project（蓝盾规范）
  url: string
  provider: string
  default_branch: string
  credential_id?: number | null
  credential_name?: string | null
}

// ---- 凭证 ----
export interface Credential {
  id: number
  project_id?: number | null
  name: string
  type: string
  description: string
  ciphertext: string
  created_at?: string
}

// ---- 项目级角色 ----
export interface RoleUser {
  id: number
  username: string
  display_name: string
}

export interface Role {
  id: number
  project_id: number
  name: string
  description: string
  permissions: Record<string, string[]>  // 资源类型 -> 操作列表
  users: RoleUser[]
}
