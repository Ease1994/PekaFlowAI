"""MCP Server：技能注册表对外暴露，权限 = 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizException
from app.db.session import get_db
from app.modules.ai.chat import execute_tool
from app.modules.ai.registry import get_skill, mcp_tools
from app.modules.ai.skills import load_all

router = APIRouter(prefix="/mcp", tags=["MCP Server"])


@router.post("/tools/list", summary="MCP 工具列表")
def mcp_tools_list(_: CurrentUser = Depends(get_current_user)):
    load_all()
    return {
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t.get("parameters") or {"type": "object", "properties": {}},
            }
            for t in mcp_tools()
        ]
    }


@router.post("/tools/call", summary="MCP 工具调用（以用户身份鉴权）")
def mcp_tools_call(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    load_all()
    tool = body.get("name", "")
    args = body.get("arguments", {})
    skill = get_skill(tool)
    if skill and skill.confirm and skill.risk == "destructive" and not str(tool).startswith("propose_"):
        if not body.get("user_confirmed"):
            raise BizException.bad_request(
                f"工具 {tool} 属于高风险操作，需要用户在界面显式确认后才能执行"
            )
    result = execute_tool(db, tool, args, current, confirmed=bool(body.get("user_confirmed")))
    return {"content": [{"type": "text", "text": str(result)}], "isError": isinstance(result, dict) and "error" in result}
