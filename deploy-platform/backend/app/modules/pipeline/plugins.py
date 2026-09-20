"""插件元数据：显示名、图标、分类（用于可视化编排）。"""
from __future__ import annotations

# 插件 -> (显示名, 图标, 分类)
PLUGIN_META: dict[str, tuple[str, str, str]] = {
    # 触发类（流水线级配置，不是构建步骤）
    "manual-trigger": ("手动触发", "▶️", "trigger"),
    "cron-trigger": ("定时触发", "⏰", "trigger"),
    "webhook-trigger": ("Webhook 触发", "🔗", "trigger"),
    # 代码类
    "git-checkout": ("Git 拉取代码", "📦", "source"),
    # 构建类
    "maven-build": ("Maven 构建", "☕", "build"),
    "gradle-build": ("Gradle 构建", "🐘", "build"),
    "npm-build": ("npm 构建", "🟩", "build"),
    "docker-build": ("Docker 镜像构建", "🐳", "build"),
    "docker-compile": ("Docker 容器编译", "🧪", "build"),
    "dotnet-publish": ("dotnet publish", "🟣", "build"),
    "msbuild-build": ("MSBuild 构建", "🟦", "build"),
    # 命令类
    "shell-exec": ("Shell 命令执行", "💻", "exec"),
    "bat-exec": ("Bat 命令执行", "🪟", "exec"),
    "python-exec": ("Python 脚本", "🐍", "exec"),
    # 制品类
    "archive-artifact": ("归档制品", "🗂️", "artifact"),
    "pack-incremental": ("提取增量发布包", "📦", "artifact"),
    # 部署类
    "k8s-deploy": ("K8s 部署", "☸️", "deploy"),
    "docker-deploy": ("Docker 部署", "🐳", "deploy"),
    "ssh-deploy": ("SSH 发送文件", "📤", "deploy"),
    "file-transfer": ("文件传输", "📁", "deploy"),
    "iis-control": ("IIS 控制", "🪟", "deploy"),
    "service-control": ("Windows 服务", "🔧", "deploy"),
    # 流水线编排
    "run-pipeline": ("运行流水线", "🔁", "pipeline"),
}


def get_plugin_meta(plugin: str) -> tuple[str, str, str]:
    """获取插件元数据，未知插件返回默认值。"""
    return PLUGIN_META.get(plugin, (plugin, "🔧", "exec"))


AGENT_ICON = {
    "linux": "🐧",
    "windows": "🪟",
    "any": "🌐",
}


def get_agent_icon(agent: str) -> str:
    return AGENT_ICON.get(agent, "🖥️")
