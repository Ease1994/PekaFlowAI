"""平台设置默认值。新增配置项只改这里，保存接口会自动认。"""
from __future__ import annotations

# 构建日志索引前缀。实际索引 rp-exec-logs-YYYY-MM-DD，按上海时区自然日新建。
DEFAULT_ES_INDEX_PREFIX = "rp-exec-logs"
# AI 审计日志独立前缀，同样按日建索引，不和构建日志混写。
DEFAULT_AI_LOG_INDEX_PREFIX = "rp-assist-logs"

DEFAULT_SETTINGS: dict[str, str] = {
    "log_storage": "es",                     # 固定 Redis 实时 + ES 归档；禁止 mysql 回退
    "es_hosts": "http://localhost:9200",     # 本机开发默认；Compose 首次写入 http://elasticsearch:9200
    "es_index": DEFAULT_ES_INDEX_PREFIX,     # 日志索引前缀，实际写入 {prefix}-YYYY-MM-DD
    "es_username": "",                       # ES 用户名（环境变量 ES_USERNAME）
    "es_password": "",                       # ES 密码（环境变量 ES_PASSWORD，勿写进仓库）
    "task_poll_interval": "2",               # Agent 拉取任务间隔（秒）
    # 构建机接入凭证：注册新构建机时必须带上（首次启动自动生成，可在构建机页面轮换）
    "agent_enroll_token": "",
    "redis_host": "localhost",               # Redis 主机（定时触发延迟队列）
    "redis_port": "6379",                    # Redis 端口
    "redis_password": "",                    # Redis 密码（可空）
    "redis_db": "0",                         # Redis 数据库编号
    # ---- LDAP 登录（含 @ 的账号走 LDAP）----
    "ldap_enabled": "false",                 # 是否启用 LDAP 登录
    "ldap_server_uri": "",                      # 启用 LDAP 后填写，例如 ldap://ldap.example.com:389
    "ldap_bind_dn": "",                      # 搜索用户的绑定账号
    "ldap_bind_password": "",                # 绑定账号密码
    "ldap_user_search_base": "ou=Users,dc=example,dc=com",
    "ldap_user_search_filter": "(|(mail={username})(userPrincipalName={username})(sAMAccountName={sam}))",
    "ldap_attr_username": "sAMAccountName",
    "ldap_attr_email": "mail",
    "ldap_attr_display_name": "displayName",
    "ldap_auto_provision": "true",          # LDAP 首次登录是否自动建档
    # ---- 企业微信登录（OAuth2）----
    "wecom_enabled": "false",               # 是否启用企微登录
    "wecom_corp_id": "",                    # 企业 ID（授权链接 appid）
    "wecom_agent_id": "",                   # 自建应用 AgentId
    "wecom_secret": "",                     # 自建应用 Secret（gettoken 用）
    "wecom_redirect_uri": "",               # OAuth 回调完整 URL（须在企微可信域名内）
    "wecom_auto_provision": "true",         # 首次登录无匹配用户时自动建档
    "wecom_notify_enabled": "false",        # 是否把待审/通报推到企微应用消息
    "wecom_app_base": "",                   # 卡片跳转的平台根地址，空则从 redirect_uri 推
    "wecom_callback_token": "",             # 企微回调 Token（配置回调 URL 用）
    "wecom_encoding_aes_key": "",           # 企微 EncodingAESKey
    # ---- 发布审批 ----
    # 平台总闸。缺省关闭：分组即使开了应急跳审也不能跳，避免新环境默认能绕过审批。
    "emergency_bypass_enabled": "false",
    # ---- 邮件通知（腾讯企业邮 / 465 SSL）----
    "smtp_enabled": "false",
    "smtp_host": "smtp.exmail.qq.com",
    "smtp_port": "465",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_from_name": "PekaFlowAI",  # 邮件/通知里的发件人显示名
    "smtp_ssl": "true",
    # ---- 品牌与登录会话 ----
    # 侧栏、登录页、浏览器标题用的平台名；侧栏图标是前端固定资源，不能在这里改
    "platform_display_name": "PekaFlowAI",
    # 顶栏图片 MIME；空表示顶栏左侧空白。文件在 data/branding/header-image，不进 value
    "header_image_mime": "",
    "header_notice_text": "",  # 顶栏通知横幅，空则不显示
    "header_notice_color": "red",  # red / orange / gold / magenta / volcano
    "session_expire_days": "1",  # 登录 JWT 有效天数，1～30，改完后新登录立刻按新值签发
    # 双因子：开了之后本地/LDAP 密码登录都要 Authenticator 码。会话未过期则不必再输。
    "totp_2fa_enabled": "false",
    # 忘记密码邮件、登录页链接用的站点根地址，例如 https://deploy.example.com
    "public_app_base": "",
    # ---- 审计 ----
    "audit_retention_days": "180",  # 审计日志保留天数，超时由定时任务清理
    # ---- Harness 隔离 Runner（第三方 Agent Tool 执行边界）----
    # 留空表示没有可用 Runner：第三方工具一律拒绝执行，不退化到本进程里跑
    "harness_runner_url": "",
    "harness_runner_token": "",
    "harness_tool_timeout_sec": "60",
    # 受信任的工具签名公钥：{"key_id": "base64(Ed25519 公钥)"}
    "harness_tool_signing_keys": "{}",
    # ---- 制品库 ----
    # 生产 / UAT / 预发：每条流水线保留最近多少天的包，窗口外仍留该线最新 2 个。测试仍只留当天。
    "artifact_prod_retention_days": "10",
}
