"""兼容旧 import：业务请走 chat / registry / skills。"""
from app.modules.ai.chat import chat, execute_tool
from app.modules.ai.registry import mcp_tools as MCP_TOOLS
from app.modules.ai.skills import load_all

load_all()

__all__ = ["chat", "execute_tool", "MCP_TOOLS"]
