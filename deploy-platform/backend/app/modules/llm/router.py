"""模型配置（管理员）+ 流水线 AI 诊断。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_admin, get_current_user
from app.core.response import R
from app.db.session import get_db
from app.modules.llm import service as llm_service
from app.modules.pipeline import diagnose as diagnose_service

router = APIRouter(tags=["大模型"])


@router.get("/llm/adapters/capabilities", summary="Adapter 运行时能力")
def adapter_capabilities(_: CurrentUser = Depends(get_current_user)):
    return R.ok(llm_service.adapter_registry.capabilities())


@router.get("/llm/adapters", summary="Adapter 列表")
def list_adapters(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(llm_service.list_adapters(db))


@router.post("/llm/adapters", summary="新增 Adapter")
def create_adapter(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.create_adapter(db, body))


@router.put("/llm/adapters/{adapter_id}", summary="更新 Adapter")
def update_adapter(
    adapter_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.update_adapter(db, adapter_id, body))


@router.delete("/llm/adapters/{adapter_id}", summary="删除 Adapter")
def delete_adapter(
    adapter_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    llm_service.delete_adapter(db, adapter_id)
    return R.ok()


@router.get("/llm/routes", summary="任务路由列表")
def list_routes(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(llm_service.list_routes(db))


@router.post("/llm/routes", summary="新增任务路由")
def create_route(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.create_route(db, body))


@router.put("/llm/routes/{route_id}", summary="更新任务路由")
def update_route(
    route_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.update_route(db, route_id, body))


@router.delete("/llm/routes/{route_id}", summary="删除任务路由")
def delete_route(
    route_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    llm_service.delete_route(db, route_id)
    return R.ok()


@router.get("/llm/observations", summary="LLM 调用观测查询")
def list_observations(
    task: str | None = None,
    status: str | None = None,
    request_id: str | None = None,
    user_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.list_observations(
        db, task=task, status=status, request_id=request_id, user_id=user_id, limit=limit,
    ))


@router.get("/llm/usage/users", summary="按用户汇总 Token 用量")
def usage_by_user(
    days: int = 7,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.usage_by_user(db, days=days))


@router.get("/llm/observations/{observation_id}", summary="LLM 调用观测详情")
def get_observation(
    observation_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.get_observation(db, observation_id))


@router.delete("/llm/observations/{observation_id}", summary="删除 LLM 调用观测")
def delete_observation(
    observation_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    llm_service.delete_observation(db, observation_id)
    return R.ok()


@router.get("/llm/providers", summary="厂商列表")
def list_providers(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(llm_service.list_providers(db))


@router.put("/llm/providers/{provider_id}", summary="更新厂商（API Key / 地址）")
def update_provider(
    provider_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.update_provider(db, provider_id, body))


@router.get("/llm/models", summary="模型列表")
def list_models(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(llm_service.list_models(db))


@router.post("/llm/models", summary="新增模型")
def create_model(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.create_model(db, body))


@router.put("/llm/models/{model_id}", summary="更新模型")
def update_model(
    model_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.update_model(db, model_id, body))


@router.delete("/llm/models/{model_id}", summary="删除模型")
def delete_model(
    model_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.delete_model(db, model_id))


@router.post("/llm/models/{model_id}/test", summary="测试指定模型是否可用")
def test_model(
    model_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.test_model(db, model_id, body))


@router.post("/llm/providers/{provider_id}/test", summary="测试厂商 Key/地址是否可用")
def test_provider(
    provider_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(llm_service.test_provider(db, provider_id, body))


@router.get("/releases/{release_id}/diagnose", summary="读取已生成的失败诊断（与通知相同）")
def get_release_diagnosis(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(diagnose_service.diagnose_release(db, release_id, current, force=False))


@router.post("/releases/{release_id}/diagnose", summary="失败流水线 AI 诊断")
def diagnose_release(
    release_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    payload = body or {}
    model_id = payload.get("model_id")
    force = bool(payload.get("force"))
    return R.ok(diagnose_service.diagnose_release(db, release_id, current, model_id, force=force))
