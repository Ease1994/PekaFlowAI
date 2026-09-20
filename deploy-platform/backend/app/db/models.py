"""统一导入所有模型，确保 Base.metadata 能发现全部表。"""
from app.modules.agent.models import BuildAgent, BuildTask, NodeGroup, NodeGroupMember
from app.modules.settings.models import PlatformSetting
from app.modules.approval.models import Approval
from app.modules.artifact.models import Artifact
from app.modules.audit.models import AuditLog
from app.modules.auth.models import ApiToken, Permission, PermissionApplication, Role, User, UserMenuDeny, UserRole
from app.modules.credential.models import Credential
from app.modules.deploy.models import DeployRequest
from app.modules.deployment.models import DeploymentRecord
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pm.models import PmDecision, ProjectMember
from app.modules.project.models import Group, Project
from app.modules.repository.models import Repository
from app.modules.store.models import PipelineTemplate, Plugin, PluginDraft
from app.modules.notify.models import InAppNotice
from app.modules.llm.models import (
    LlmAdapterConfig,
    LlmModel,
    LlmObservation,
    LlmProvider,
    LlmTaskRoute,
)
from app.modules.ai.models import (
    AiAttachment,
    AiConversation,
    AiMessage,
    AiSession,
    AiSessionEvent,
    AiWatch,
)
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessToolInvocation,
    HarnessVersion,
)

__all__ = [
    "User",
    "UserMenuDeny",
    "Permission",
    "PermissionApplication",
    "Role",
    "UserRole",
    "ApiToken",
    "Project",
    "Group",
    "Repository",
    "Credential",
    "DeployRequest",
    "DeploymentRecord",
    "Pipeline",
    "Release",
    "BuildAgent",
    "BuildTask",
    "NodeGroup",
    "NodeGroupMember",
    "PlatformSetting",
    "Plugin",
    "PluginDraft",
    "PipelineTemplate",
    "Artifact",
    "Approval",
    "AuditLog",
    "LlmProvider",
    "LlmModel",
    "LlmAdapterConfig",
    "LlmTaskRoute",
    "LlmObservation",
    "AiConversation",
    "AiMessage",
    "AiSession",
    "AiSessionEvent",
    "AiAttachment",
    "AiWatch",
    "InAppNotice",
    "ProjectMember",
    "PmDecision",
    "HarnessComponent",
    "HarnessVersion",
    "HarnessDependency",
    "HarnessRuntime",
    "HarnessToolInvocation",
    "HarnessLifecycleAudit",
]
