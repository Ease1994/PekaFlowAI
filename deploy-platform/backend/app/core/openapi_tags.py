"""调用手册里的接口分类。

Swagger / OpenAPI 按 tag 分组。名称必须和各路由 APIRouter(tags=[...]) 一字不差，
顺序按侧栏模块：工作台 → 资源 → 系统 → 登录接入，方便按分类找接口。
"""

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "项目与分组", "description": "项目、环境分组"},
    {"name": "项目经理", "description": "项目工作台"},
    {"name": "发布提交", "description": "发起发布、提交记录"},
    {"name": "审批管理", "description": "发布审批"},
    {"name": "权限管理", "description": "授权、侧栏菜单可见范围"},
    {"name": "权限申请", "description": "申请访问项目或流水线"},
    {"name": "流水线编排", "description": "流水线、步骤、执行、画布"},
    {"name": "代码仓库", "description": "关联 Git 仓库"},
    {"name": "制品管理", "description": "构建产物"},
    {"name": "构建机管理", "description": "构建机与部署节点"},
    {"name": "凭证管理", "description": "镜像仓库、SSH 等密文"},
    {"name": "研发商店", "description": "流水线插件"},
    {"name": "大模型", "description": "模型与供应商"},
    {"name": "AI 助手", "description": "对话、工具调用"},
    {"name": "调用手册", "description": "登录后读取本 OpenAPI 文档"},
    {"name": "指标大盘", "description": "发布频率、失败率等 DORA 指标"},
    {"name": "用户管理", "description": "账号"},
    {"name": "角色管理", "description": "角色与权限模板"},
    {"name": "平台配置", "description": "LDAP、邮件、品牌等"},
    {"name": "审计日志", "description": "操作记录"},
    {"name": "通知中心", "description": "站内通知"},
    {"name": "系统", "description": "依赖健康检查"},
    {"name": "认证", "description": "登录、会话、找回密码"},
    {"name": "API Token", "description": "脚本/CI 调用用的令牌"},
    {"name": "企业微信登录", "description": "企微 OAuth"},
    {"name": "Harness", "description": "插件运行器"},
    {"name": "MCP Server", "description": "给外部 AI 客户端的 MCP 端点"},
]
