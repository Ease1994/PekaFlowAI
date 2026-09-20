"""进程启动配置：只放连库、密钥等「不启动就起不来」的项。

运营项（Redis / LDAP / 企微 / SMTP / ES / Agent 轮询）在 app.modules.settings，
管理员在「平台设置」页修改，立即生效，不要写进本文件。
"""
import json
import secrets as _secrets
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 历史上作为默认值发过的密钥。它们进过 git、进过 .env.example，等于公开——
# 谁拿到源码谁就能伪造管理员 token、解开凭证密文。默认值一旦泄露就只能列入黑名单，
# 命中就换掉，绝不能再当有效值用。
_LEAKED_JWT = "deploy-platform-jwt-secret-key-must-be-long-enough-for-hs256-2026"
_LEAKED_AES = "deploy-platform-32bytes-aes-key!!"

# 自动生成的密钥落在数据目录，跟着 backend-data 卷走，重启保持不变。
# config.py 在 backend/app/core/ 下，parents[2] 即 backend/
_SECRETS_FILE = Path(__file__).resolve().parents[2] / "data" / ".secrets.json"


def _load_or_create_secret(name: str, length: int) -> str:
    """从持久化文件读密钥，没有就生成一把并落盘。

    这样 `docker compose up` 开箱即用——不用手工配密钥，但每套部署拿到的都是
    各自独立的随机值，而不是全世界共用一个源码里的默认值。
    """
    try:
        if _SECRETS_FILE.exists():
            data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
            val = data.get(name)
            if isinstance(val, str) and val:
                return val
        else:
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    val = _secrets.token_urlsafe(length)
    data[name] = val
    try:
        _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # 尽力收紧权限；Windows 上 chmod 基本无效，忽略即可
        try:
            _SECRETS_FILE.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        print(f"[startup] 警告：密钥无法持久化（{e}），本次用内存里的随机值，"
              "重启后所有人需要重新登录", file=sys.stderr)
    return val


class Settings(BaseSettings):
    """部署级配置，来自环境变量 / .env。未知变量忽略（兼容旧 REDIS_URL、LLM_*）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PekaFlowAI"
    app_version: str = "0.1.0"
    debug: bool = False
    # 接口文档（/docs、/swagger-ui）。默认关：外网入口 Nginx 不转发，生产也不该开。
    # 登录后的「调用手册」不依赖这项；需要 Swagger UI 时再设 API_DOCS_ENABLED=true。
    api_docs_enabled: bool = False

    # 进程启动即绑定，改了必须重启。生产用环境变量 DATABASE_URL。
    database_url: str = "sqlite:///./deploy_platform.db"

    # 留空表示「没显式配」，交给下面的校验器去持久化文件里取或生成。
    # 不再给硬编码默认值——那等于把管理员钥匙印在源码里。
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 86400

    # 凭证密文加密，改了旧凭证解不开。同样不给硬编码默认。
    aes_key: str = ""

    @model_validator(mode="after")
    def _resolve_secrets(self) -> "Settings":
        # 显式配了就用配的，但命中泄露过的旧默认值一律当没配：那串值是公开的，
        # 留着比不配还危险。debug 下放行，方便本地起服务
        if self.jwt_secret and (self.jwt_secret == _LEAKED_JWT and not self.debug):
            print("[startup] 警告：JWT_SECRET 用的是已泄露的旧默认值，已改用自动生成的密钥。"
                  "所有人需要重新登录", file=sys.stderr)
            self.jwt_secret = ""
        if self.aes_key and (self.aes_key == _LEAKED_AES and not self.debug):
            print("[startup] 警告：AES_KEY 用的是已泄露的旧默认值，已改用自动生成的密钥。"
                  "此前用旧默认值加密保存的凭证需要重新录入", file=sys.stderr)
            self.aes_key = ""

        if not self.jwt_secret:
            self.jwt_secret = (_LEAKED_JWT if self.debug
                               else _load_or_create_secret("jwt_secret", 48))
        if not self.aes_key:
            self.aes_key = (_LEAKED_AES if self.debug
                            else _load_or_create_secret("aes_key", 32))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def database_display() -> str:
    """给设置页只读展示当前连的是哪套库（不含密码）。"""
    url = settings.database_url or ""
    if url.startswith("sqlite"):
        return "sqlite " + (url.split("///")[-1] or "./deploy_platform.db")
    try:
        from urllib.parse import urlparse

        parsed = urlparse(
            url.replace("mysql+pymysql://", "http://", 1).replace("postgresql+psycopg2://", "http://", 1)
        )
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = (parsed.path or "").lstrip("/").split("?")[0]
        return f"{host}{port}/{path}"
    except Exception:  # noqa: BLE001
        return "(已配置 DATABASE_URL)"
