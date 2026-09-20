"""种子数据：首次启动（空库）时写入演示项目和三条典型流水线，用户只建 admin。

真实用户由 LDAP / 企微登录或管理员在「用户管理」创建，姓名和邮箱以目录为准，不在代码里写死任何人。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import Group, Pipeline, Plugin, Project, Repository, User

DEMO_PASSWORD = "admin123"


def seed(db: Session) -> None:
    """写入「发布示例项目」和三类发布流水线；用户仅 admin。"""
    pwd = hash_password(DEMO_PASSWORD)
    admin = User(
        username="admin",
        display_name="系统管理员",
        email="",
        password_hash=pwd,
        is_admin=True,
        source="local",
    )
    db.add(admin)
    db.flush()

    project = Project(
        name="发布示例项目",
        code="DEMO",
        description="容器化、Windows 增量、前端三类发布的示例流水线",
    )
    db.add(project)
    db.flush()

    prod = Group(
        project_id=project.id, name="生产", type="prod",
        approval_required=True, allow_self_approval=True,
    )
    test = Group(project_id=project.id, name="测试", type="test", approval_required=False)
    db.add_all([prod, test])
    db.flush()

    db.add_all([
        Repository(
            project_id=project.id, name="demo-java",
            url="https://git.example.com/demo/java-service.git",
            provider="git", default_branch="master",
        ),
        Repository(
            project_id=project.id, name="demo-dotnet",
            url="https://git.example.com/demo/windows-app.git",
            provider="git", default_branch="master",
        ),
        Repository(
            project_id=project.id, name="demo-web",
            url="https://git.example.com/demo/web.git",
            provider="git", default_branch="master",
        ),
    ])

    # 真实实现由 sync_builtin_plugins 从 plugins/ 打 zip 登记。
    # 这里只放 Agent jar 内置、没有 zip 的步骤，避免再插入「已安装但跑不了」的空壳。
    plugins_data = [
        ("shell-exec", "Shell 命令执行", "exec", "在构建机上执行 Shell 脚本", _SCHEMA_SHELL_EXEC),
        ("bat-exec", "Bat 命令执行", "exec", "在 Windows 构建机上执行 Bat", None),
    ]
    existing_plugins = set(db.scalars(select(Plugin.name)).all())
    db.add_all([
        Plugin(
            name=n, display_name=d, category=c, description=desc,
            config_schema=json.dumps(s, ensure_ascii=False) if s else "{}",
            installed=True,
            enabled=True,
            status="installed",
        )
        for n, d, c, desc, s in plugins_data
        if n not in existing_plugins
    ])

    db.add_all([
        _demo_pipeline(
            project_id=project.id, group_id=prod.id, created_by=admin.id,
            name="容器化发布",
            description="Java Maven 构建镜像并部署容器",
            yaml=_YAML_CONTAINER,
        ),
        _demo_pipeline(
            project_id=project.id, group_id=prod.id, created_by=admin.id,
            name="Windows增量发布",
            description="dotnet publish 后打增量包，停 IIS、发文件、再启动",
            yaml=_YAML_WINDOWS,
        ),
        _demo_pipeline(
            project_id=project.id, group_id=prod.id, created_by=admin.id,
            name="前端发布",
            description="检出前端代码，打增量包并发送到节点",
            yaml=_YAML_FRONTEND,
        ),
    ])

    db.commit()
    print("[seed] 演示数据已写入：1 用户(admin) / 1 项目 / 2 分组 / 3 流水线")


def _demo_pipeline(
    *,
    project_id: int,
    group_id: int,
    created_by: int,
    name: str,
    description: str,
    yaml: str,
) -> Pipeline:
    """一条示例流水线：画布编排、手工触发，工作区 uuid 当场生成。"""
    return Pipeline(
        project_id=project_id,
        group_id=group_id,
        name=name,
        description=description,
        yaml=yaml,
        version=1,
        created_by=created_by,
        workspace_uuid=uuid.uuid4().hex,
        editor_view="canvas",
        trigger_type="manual",
    )


# ============================================================
# 示例 YAML：结构和页面上三类发布一致。仓库 URL、节点、站点目录要自己改。
# 增量包清单保持 ${{DEPLOY_MANIFEST}}，执行时必须填写，不会静默全量打包。
# ============================================================
_YAML_CONTAINER = """\
pipeline:
  name: 容器化发布
  triggers:
    - type: manual
  stages:
    - name: stage-1
      jobs:
        - id: "1-1"
          name: 构建环境-Linux
          agent: linux
          steps:
            - name: Git 拉取代码
              plugin: git-checkout
              with:
                repoName: demo-java
                ref: master
            - name: Maven 构建
              plugin: maven-build
              with:
                goal: package
                skipTests: true
            - name: Docker 镜像构建
              plugin: docker-build
              with:
                image: registry.example.com/demo/app
                tag: ${{BK_CI_BUILD_NUM}}
            - name: Docker 容器部署
              plugin: docker-deploy
              with:
                image: registry.example.com/demo/app:${{BK_CI_BUILD_NUM}}
                containerName: demo-app
"""

_YAML_WINDOWS = """\
pipeline:
  name: Windows增量发布
  triggers:
    - type: manual
  stages:
    - name: stage-1
      jobs:
        - id: "1-1"
          name: 构建环境-Windows
          agent: windows
          steps:
            - name: Git 拉取代码
              plugin: git-checkout
              with:
                repoName: demo-dotnet
                ref: master
            - name: dotnet publish
              plugin: dotnet-publish
              with:
                project: Demo.Web/Demo.Web.csproj
                outputDir: _publish
            - name: 提取增量发布包
              plugin: pack-incremental
              with:
                sourceDir: _publish
                manifest: ${{DEPLOY_MANIFEST}}
            - name: 停止应用池
              plugin: iis-control
              with:
                action: stop
                target: apppool
                name: DemoAppPool
            - name: 停止站点
              plugin: iis-control
              with:
                action: stop
                target: site
                name: DemoSite
            - name: 文件传输
              plugin: file-transfer
              with:
                targetDir: "D:/wwwroot/demo"
                keepBackups: 20
            - name: 启动应用池
              plugin: iis-control
              with:
                action: start
                target: apppool
                name: DemoAppPool
            - name: 启动站点
              plugin: iis-control
              with:
                action: start
                target: site
                name: DemoSite
"""

_YAML_FRONTEND = """\
pipeline:
  name: 前端发布
  triggers:
    - type: manual
  stages:
    - name: 拉取代码
      jobs:
        - id: "1-1"
          name: 构建环境-Linux
          agent: linux
          steps:
            - name: Git 拉取代码
              plugin: git-checkout
              with:
                repoName: demo-web
                ref: master
            - name: 提取增量发布包
              plugin: pack-incremental
              with:
                sourceDir: dist
                manifest: ${{DEPLOY_MANIFEST}}
            - name: 发送文件到节点
              plugin: file-transfer
              with:
                targetDir: /data/nginx/html/demo
                keepBackups: 20
"""


def _schema(*fields):
    return {"fields": list(fields)}


def _opt(value, label):
    return {"value": value, "label": label}


_SCHEMA_SHELL_EXEC = _schema(
    {"key": "shellType", "label": "脚本类型", "type": "radio", "required": True,
     "options": [_opt("shell", "Shell")], "default": "shell"},
    {"key": "content", "label": "脚本内容", "type": "code", "required": True, "rows": 12,
     "placeholder": "#!/usr/bin/env bash\nset -e\necho 'hello'"},
    {"key": "continueOnError", "label": "失败时继续", "type": "checkbox", "default": False},
    {"key": "uploadOnError", "label": "失败时上传日志", "type": "checkbox", "default": False},
)
