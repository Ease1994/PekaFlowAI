"""平台运行时配置：管理员在「平台设置」里改，立即生效。

启动级（DATABASE_URL / JWT / AES）见 app.core.config，改了必须重启进程。
"""

from app.modules.settings.defaults import DEFAULT_SETTINGS
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import get_all_settings, get_setting, update_settings

__all__ = [
    "DEFAULT_SETTINGS",
    "PlatformSetting",
    "get_all_settings",
    "get_setting",
    "update_settings",
]
