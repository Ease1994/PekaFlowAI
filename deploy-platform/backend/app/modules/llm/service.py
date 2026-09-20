"""LLM catalog, adapter registry, task routing and observations."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import decrypt, encrypt
from app.core.response import BizException
from app.modules.credential.models import Credential
from app.modules.llm.catalog import BUILTIN_MODELS, BUILTIN_PROVIDERS
from app.modules.llm.client import (
    AdapterResponse,
    FailureKind,
    LlmAdapter,
    LlmClient,
    LlmFailure,
    LlmInvokeConfig,
    Message,
    OpenAICompatibleAdapter,
    UnavailableAdapter,
)
from app.modules.llm.models import (
    LlmAdapterConfig,
    LlmModel,
    LlmObservation,
    LlmProvider,
    LlmTaskRoute,
)

TASKS = {"assistant", "diagnose", "authoring", "summary"}
AdapterFactory = Callable[[LlmInvokeConfig], LlmAdapter]
logger = logging.getLogger(__name__)


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, protocol: str, factory: AdapterFactory) -> None:
        self._factories[protocol] = factory

    def create(self, protocol: str, config: LlmInvokeConfig) -> LlmAdapter:
        factory = self._factories.get(protocol)
        if factory is None:
            return UnavailableAdapter(protocol, "native protocol is not implemented")
        return factory(config)

    def capabilities(self) -> list[dict]:
        reserved = {"deepseek-native", "anthropic-native", "bedrock-native"}
        return [
            {
                "protocol": name,
                "available": name in self._factories,
                "reason": "" if name in self._factories else "native protocol is not implemented",
            }
            for name in sorted(set(self._factories) | reserved)
        ]


adapter_registry = AdapterRegistry()
adapter_registry.register("openai-compatible", OpenAICompatibleAdapter)


def mask_key(key: str | None) -> str:
    raw = (key or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return raw[:2] + "***"
    return raw[:4] + "***" + raw[-4:]


def provider_public(p: LlmProvider) -> dict:
    has_secret = bool(p.credential_id or (p.api_key or "").strip())
    return {
        "id": p.id,
        "name": p.name,
        "code": p.code,
        "api_base_url": p.api_base_url or "",
        "credential_id": p.credential_id,
        "adapter_id": p.adapter_id,
        "api_key_masked": "***" if has_secret else "",
        "has_api_key": has_secret,
        "description": p.description or "",
        "website": p.website or "",
        "is_active": bool(p.is_active),
        "sort_order": p.sort_order or 0,
    }


# 看图能力没法从厂商接口问出来，只能靠人工标或者按名字认。
# 这些片段是各家多模态模型名里的稳定特征，认错的代价只是多传一次图片被拒。
_VISION_HINTS = (
    "vl", "vision", "-v-", "4v", "omni", "multimodal",
    "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "claude-4", "claude-opus", "claude-sonnet",
    "gemini", "llava", "internvl", "pixtral", "grok-2-vision", "grok-4",
)


def guess_capabilities(model_id: str, name: str = "") -> list[str]:
    blob = f"{model_id} {name}".lower()
    return ["vision"] if any(h in blob for h in _VISION_HINTS) else []


def model_capabilities(m: LlmModel) -> list[str]:
    """人工标过就以人工为准，没标过按名字猜，让存量模型不用逐个去点一遍。"""
    raw = (getattr(m, "capabilities", "") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
        return []
    return guess_capabilities(m.model_id or "", m.name or "")


def model_public(m: LlmModel, provider: LlmProvider | None = None) -> dict:
    p = provider or m.provider
    capabilities = model_capabilities(m)
    return {
        "capabilities": capabilities,
        "capabilities_source": "manual" if (getattr(m, "capabilities", "") or "").strip() else "guess",
        "supports_vision": "vision" in capabilities,
        "id": m.id,
        "name": m.name,
        "model_id": m.model_id,
        "provider_id": m.provider_id,
        "provider_code": p.code if p else "",
        "provider_name": p.name if p else "",
        "credential_id": m.credential_id,
        "adapter_id": m.adapter_id,
        "description": m.description or "",
        "max_tokens": m.max_tokens,
        "context_window": m.context_window or 0,
        "temperature": m.temperature,
        "is_active": bool(m.is_active),
        "is_default": bool(m.is_default),
        "sort_order": m.sort_order or 0,
        "ready": bool(
            p and p.is_active
            and (m.credential_id or p.credential_id or (p.api_key or "").strip())
            and (p.api_base_url or "").strip()
        ),
    }


def _credential_secret(db: Session, credential_id: int | None) -> str:
    if not credential_id:
        return ""
    credential = db.get(Credential, credential_id)
    if credential is None:
        raise BizException.bad_request(f"凭证 #{credential_id} 不存在")
    try:
        return decrypt(credential.ciphertext, credential.iv)
    except Exception as exc:  # noqa: BLE001
        raise BizException.bad_request(f"凭证 #{credential_id} 无法解密") from exc


def _provider_secret(db: Session, provider: LlmProvider, model: LlmModel | None = None) -> str:
    credential_id = (model.credential_id if model else None) or provider.credential_id
    if credential_id:
        return _credential_secret(db, credential_id)
    # Transitional read compatibility. Never expose this value through API responses.
    return (provider.api_key or "").strip()


_OUTDATED_TENCENT_DS = {
    "deepseek-v3.2",
    "deepseek-v3-0324",
    "deepseek-r1-0528",
    "deepseek-v3.1-terminus",
}
_OLD_LKEAP_URLS = {
    "https://api.lkeap.cloud.tencent.com/v1",
    "https://api.lkeap.cloud.tencent.com/v3",
}
_DEFAULT_TENCENT_DS_URL = "https://tokenhub.tencentmaas.com/v1"


def seed_llm_catalog(db: Session) -> None:
    """补齐内置厂商/模型；已有记录不覆盖用户改过的 Key。V4 Pro 作为腾讯云 DeepSeek 默认。"""
    default_adapter = db.scalar(
        select(LlmAdapterConfig).where(LlmAdapterConfig.code == "openai-compatible")
    )
    if default_adapter is None:
        default_adapter = LlmAdapterConfig(
            name="OpenAI Compatible",
            code="openai-compatible",
            protocol="openai-compatible",
            is_active=True,
        )
        db.add(default_adapter)
        db.flush()
    by_code = {p.code: p for p in db.scalars(select(LlmProvider)).all()}
    for code, name, website, desc, base, order in BUILTIN_PROVIDERS:
        if code in by_code:
            continue
        p = LlmProvider(
            code=code,
            name=name,
            website=website,
            description=desc,
            api_base_url=base,
            api_key="",
            adapter_id=default_adapter.id,
            is_active=True,
            sort_order=order,
        )
        db.add(p)
        by_code[code] = p
    db.flush()

    tencent_ds = by_code.get("tencent-lkeap")
    if tencent_ds:
        current_url = (tencent_ds.api_base_url or "").strip().rstrip("/")
        if current_url in {u.rstrip("/") for u in _OLD_LKEAP_URLS} or not current_url:
            tencent_ds.api_base_url = _DEFAULT_TENCENT_DS_URL
        if "LKEAP" in (tencent_ds.description or "") or not (tencent_ds.description or "").strip():
            tencent_ds.description = next(
                (d for c, _n, _w, d, _b, _o in BUILTIN_PROVIDERS if c == "tencent-lkeap"),
                tencent_ds.description,
            )
    for provider in by_code.values():
        if provider.adapter_id is None:
            provider.adapter_id = default_adapter.id

    existing_models = {
        (m.provider.code if m.provider else "", m.model_id): m
        for m in db.scalars(select(LlmModel).options(selectinload(LlmModel.provider))).all()
    }
    v4_was_missing = ("tencent-lkeap", "deepseek-v4-pro") not in existing_models
    has_default = any(m.is_default for m in existing_models.values())
    for pcode, name, mid, desc, tokens, is_default, order in BUILTIN_MODELS:
        p = by_code.get(pcode)
        if p is None or (pcode, mid) in existing_models:
            continue
        row = LlmModel(
            name=name,
            model_id=mid,
            provider_id=p.id,
            description=desc,
            max_tokens=tokens,
            temperature=0.2,
            is_active=True,
            is_default=bool(is_default) and not has_default,
            sort_order=order,
        )
        db.add(row)
        existing_models[(pcode, mid)] = row
        if is_default and not has_default:
            has_default = True
    db.flush()

    for (pcode, mid), m in list(existing_models.items()):
        if pcode == "tencent-lkeap" and mid in _OUTDATED_TENCENT_DS:
            m.is_active = False
            m.is_default = False

    v4 = existing_models.get(("tencent-lkeap", "deepseek-v4-pro"))
    current_default = next((m for m in existing_models.values() if m.is_default), None)
    outdated_default = current_default is None or current_default.model_id in _OUTDATED_TENCENT_DS
    if v4 is not None and (v4_was_missing or outdated_default):
        v4.is_active = True
        _clear_default(db)
        v4.is_default = True
    db.commit()


def list_providers(db: Session) -> list[dict]:
    rows = db.scalars(select(LlmProvider).order_by(LlmProvider.sort_order, LlmProvider.id)).all()
    return [provider_public(p) for p in rows]


def list_models(db: Session) -> list[dict]:
    rows = db.scalars(
        select(LlmModel)
        .options(selectinload(LlmModel.provider))
        .order_by(LlmModel.sort_order, LlmModel.id)
    ).all()
    return [model_public(m) for m in rows]


def list_usable_models(db: Session) -> list[dict]:
    """给普通用户挑模型用：只出真正能跑的，且不带任何凭证信息。

    /llm/models 是管理员接口，返回 credential_id、api_key_masked 这些东西，
    不能直接开放给对话框。
    """
    rows = db.scalars(
        select(LlmModel)
        .options(selectinload(LlmModel.provider))
        .where(LlmModel.is_active.is_(True))
        .order_by(LlmModel.sort_order, LlmModel.id)
    ).all()
    out = []
    for m in rows:
        info = model_public(m)
        if not info["ready"]:
            continue
        out.append(
            {
                "id": info["id"],
                "name": info["name"],
                "model_id": info["model_id"],
                "provider_name": info["provider_name"],
                "description": info["description"],
                "max_tokens": info["max_tokens"],
                "context_window": info["context_window"],
                "capabilities": info["capabilities"],
                "supports_vision": info["supports_vision"],
                "is_default": info["is_default"],
            }
        )
    return out


def usable_model_id(db: Session, model_pk: int | None) -> int | None:
    """对话框允许用的模型主键。停用、没 Key、厂商关掉的一律不算。"""
    if not model_pk:
        return None
    wanted = int(model_pk)
    for item in list_usable_models(db):
        if item["id"] == wanted:
            return wanted
    return None


def update_provider(db: Session, provider_id: int, body: dict) -> dict:
    p = db.get(LlmProvider, provider_id)
    if p is None:
        raise BizException.not_found("厂商")
    if "name" in body and body["name"]:
        p.name = str(body["name"]).strip()
    if "api_base_url" in body:
        p.api_base_url = str(body.get("api_base_url") or "").strip()
    if "description" in body:
        p.description = body.get("description") or ""
    if "is_active" in body:
        p.is_active = bool(body["is_active"])
    if "adapter_id" in body:
        p.adapter_id = int(body["adapter_id"]) if body["adapter_id"] else None
    if "credential_id" in body:
        credential_id = int(body["credential_id"]) if body["credential_id"] else None
        if credential_id and db.get(Credential, credential_id) is None:
            raise BizException.not_found("凭证")
        p.credential_id = credential_id
    secret = body.get("api_key")
    if secret is not None and str(secret).strip() and "***" not in str(secret):
        ciphertext, iv = encrypt(str(secret).strip())
        credential = (
            db.get(Credential, p.credential_id)
            if p.credential_id else None
        )
        if credential is None:
            credential = Credential(
                project_id=None,
                name=f"LLM/{p.name}",
                type="api_key",
                ciphertext=ciphertext,
                iv=iv,
                description=f"LLM 厂商 {p.code} 的 API Key",
                created_by=None,
            )
            db.add(credential)
            db.flush()
            p.credential_id = credential.id
        else:
            credential.ciphertext = ciphertext
            credential.iv = iv
        p.api_key = ""
    db.commit()
    db.refresh(p)
    return provider_public(p)


def create_model(db: Session, body: dict) -> dict:
    provider_id = int(body.get("provider_id") or 0)
    p = db.get(LlmProvider, provider_id)
    if p is None:
        raise BizException.not_found("厂商")
    m = LlmModel(
        name=str(body.get("name") or body.get("model_id") or "未命名"),
        model_id=str(body.get("model_id") or "").strip(),
        provider_id=provider_id,
        credential_id=int(body["credential_id"]) if body.get("credential_id") else None,
        adapter_id=int(body["adapter_id"]) if body.get("adapter_id") else None,
        description=body.get("description") or "",
        max_tokens=int(body.get("max_tokens") or 4096),
        context_window=int(body.get("context_window") or 0),
        temperature=float(body.get("temperature") or 0.2),
        capabilities=_capabilities_field(body),
        is_active=bool(body.get("is_active", True)),
        is_default=bool(body.get("is_default", False)),
        sort_order=int(body.get("sort_order") or 0),
    )
    if not m.model_id:
        raise BizException.bad_request("缺少 model_id")
    if m.is_default:
        _clear_default(db)
    db.add(m)
    db.commit()
    db.refresh(m)
    return model_public(m, p)


def update_model(db: Session, model_id: int, body: dict) -> dict:
    m = db.get(LlmModel, model_id)
    if m is None:
        raise BizException.not_found("模型")
    for field in ("name", "description"):
        if field in body and body[field] is not None:
            setattr(m, field, str(body[field]))
    if "model_id" in body and body["model_id"] is not None:
        mid = str(body["model_id"]).strip()
        if not mid:
            raise BizException.bad_request("缺少 model_id")
        m.model_id = mid
    if "max_tokens" in body and body["max_tokens"] is not None:
        m.max_tokens = int(body["max_tokens"])
    if "context_window" in body and body["context_window"] is not None:
        m.context_window = int(body["context_window"])
    if "temperature" in body and body["temperature"] is not None:
        m.temperature = float(body["temperature"])
    if "capabilities" in body:
        m.capabilities = _capabilities_field(body)
    if "is_active" in body:
        m.is_active = bool(body["is_active"])
    if "is_default" in body and body["is_default"]:
        _clear_default(db)
        m.is_default = True
    elif "is_default" in body:
        m.is_default = bool(body["is_default"])
    if "provider_id" in body and body["provider_id"]:
        m.provider_id = int(body["provider_id"])
    for field in ("credential_id", "adapter_id"):
        if field in body:
            setattr(m, field, int(body[field]) if body[field] else None)
    db.commit()
    db.refresh(m)
    return model_public(m)


def delete_model(db: Session, model_id: int) -> dict:
    """删掉一条模型目录。

    默认模型要先换默认再删。任务路由的主模型占用时禁止删。
    只出现在回退列表里的，先从各条路由里摘掉再删目录。
    """
    m = db.get(LlmModel, model_id)
    if m is None:
        raise BizException.not_found("模型")
    if m.is_default:
        raise BizException.bad_request("不能删除默认模型，请先把别的模型设为默认")
    used = db.scalars(select(LlmTaskRoute).where(LlmTaskRoute.model_id == model_id)).all()
    if used:
        names = "、".join(row.task for row in used)
        raise BizException.bad_request(f"任务路由「{names}」还在用这个模型，请先改路由再删")
    for row in db.scalars(select(LlmTaskRoute)).all():
        ids = [i for i in row.fallback_model_ids() if i != model_id]
        if ids != row.fallback_model_ids():
            row.fallback_model_ids_json = json.dumps(ids)
    db.delete(m)
    db.commit()
    return {"id": model_id}


def _capabilities_field(body: dict) -> str:
    """没给就留空串走名字推断；给了空列表就是明确声明不支持，必须存成 "[]"。"""
    if "capabilities" not in body or body["capabilities"] is None:
        return ""
    value = body["capabilities"]
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    return json.dumps(sorted({str(x) for x in value}), ensure_ascii=False)


def _clear_default(db: Session) -> None:
    for row in db.scalars(select(LlmModel).where(LlmModel.is_default.is_(True))).all():
        row.is_default = False


def resolve_invoke(db: Session, model_pk: int | None = None) -> tuple[LlmModel, LlmProvider]:
    m: LlmModel | None = None
    if model_pk:
        m = db.get(LlmModel, int(model_pk))
    if m is None:
        m = db.scalar(select(LlmModel).where(LlmModel.is_default.is_(True), LlmModel.is_active.is_(True)))
    if m is None:
        m = db.scalar(select(LlmModel).where(LlmModel.is_active.is_(True)).order_by(LlmModel.sort_order))
    if m is None:
        raise BizException.bad_request("尚未配置可用模型，请到「模型配置」添加")
    p = db.get(LlmProvider, m.provider_id)
    if p is None or not p.is_active:
        raise BizException.bad_request(f"模型 {m.name} 所属厂商未启用")
    if not (p.api_base_url or "").strip():
        raise BizException.bad_request(f"厂商 {p.name} 未填写 API 地址")
    if not (m.credential_id or p.credential_id or (p.api_key or "").strip()):
        raise BizException.bad_request(f"厂商 {p.name} 未填写 API Key，请到「模型配置」填写")
    return m, p


def build_client(db: Session, model_pk: int | None = None) -> tuple[LlmClient, dict]:
    m, p = resolve_invoke(db, model_pk)
    adapter_row = db.get(LlmAdapterConfig, m.adapter_id or p.adapter_id) if (m.adapter_id or p.adapter_id) else None
    protocol = adapter_row.protocol if adapter_row else "openai-compatible"
    base_url = (adapter_row.api_base_url if adapter_row else "") or p.api_base_url
    secret = _credential_secret(db, adapter_row.credential_id) if adapter_row and adapter_row.credential_id else _provider_secret(db, p, m)
    config = LlmInvokeConfig(
        api_base_url=base_url,
        api_key=secret,
        model=m.model_id,
        max_tokens=min(int(m.max_tokens or 2048), 8192),
        temperature=float(m.temperature or 0.2),
        context_window=m.context_window or None,
        extra=adapter_row.settings() if adapter_row else {},
    )
    client = LlmClient(
        config,
        adapter_registry.create(protocol, config),
    )
    return client, model_public(m, p)


def _pick_provider_model(db: Session, provider_id: int) -> LlmModel:
    m = db.scalar(
        select(LlmModel)
        .where(LlmModel.provider_id == provider_id, LlmModel.is_active.is_(True))
        .order_by(LlmModel.is_default.desc(), LlmModel.sort_order, LlmModel.id)
    )
    if m is None:
        raise BizException.bad_request("该厂商下没有启用的模型，请先在「模型」里添加")
    return m


def test_model(db: Session, model_pk: int, body: dict | None = None) -> dict:
    m = db.get(LlmModel, int(model_pk))
    if m is None:
        raise BizException.not_found("模型")
    p = db.get(LlmProvider, m.provider_id)
    if p is None:
        raise BizException.not_found("厂商")
    return _run_ping(db, m, p, body or {})


def test_provider(db: Session, provider_id: int, body: dict | None = None) -> dict:
    p = db.get(LlmProvider, int(provider_id))
    if p is None:
        raise BizException.not_found("厂商")
    payload = body or {}
    model_pk = payload.get("model_pk")
    m = db.get(LlmModel, int(model_pk)) if model_pk else _pick_provider_model(db, p.id)
    if m is None or m.provider_id != p.id:
        raise BizException.bad_request("模型不属于该厂商")
    return _run_ping(db, m, p, payload)


def _run_ping(db: Session, m: LlmModel, p: LlmProvider, body: dict) -> dict:
    import time

    # A one-shot test key is never persisted or returned.
    api_key = str(body.get("api_key") or "").strip()
    if not api_key:
        credential_id = body.get("credential_id") or m.credential_id or p.credential_id
        api_key = _credential_secret(db, int(credential_id)) if credential_id else (p.api_key or "").strip()
    base = str(body.get("api_base_url") or "").strip() or (p.api_base_url or "").strip()
    if not base:
        raise BizException.bad_request(f"厂商 {p.name} 未填写 API 地址")
    if not api_key:
        raise BizException.bad_request(f"厂商 {p.name} 未填写 API Key")
    client = LlmClient(
        LlmInvokeConfig(
            api_base_url=base,
            api_key=api_key,
            model=m.model_id,
            max_tokens=64,
            temperature=0,
            timeout_sec=25,
        )
    )
    t0 = time.perf_counter()
    try:
        reply = client.chat(
            [
                {"role": "system", "content": "你是连通性探测，只回复两个字：可用"},
                {"role": "user", "content": "ping"},
            ],
            timeout_sec=25,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        ok = bool(str(reply).strip())
        return {
            "ok": ok,
            "latency_ms": ms,
            "reply": str(reply).strip()[:500],
            "error": "" if ok else "模型返回空内容",
            "provider_name": p.name,
            "model_name": m.name,
            "model_id": m.model_id,
        }
    except Exception as e:  # noqa: BLE001
        ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "latency_ms": ms,
            "reply": "",
            "error": str(e)[:800],
            "provider_name": p.name,
            "model_name": m.name,
            "model_id": m.model_id,
        }


def adapter_public(row: LlmAdapterConfig) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "code": row.code,
        "protocol": row.protocol,
        "api_base_url": row.api_base_url or "",
        "credential_id": row.credential_id,
        "has_credential": bool(row.credential_id),
        "settings": row.settings(),
        "is_active": bool(row.is_active),
        "runtime": next(
            (
                capability for capability in adapter_registry.capabilities()
                if capability["protocol"] == row.protocol
            ),
            {"protocol": row.protocol, "available": False, "reason": "adapter is not registered"},
        ),
    }


def list_adapters(db: Session) -> list[dict]:
    return [adapter_public(row) for row in db.scalars(
        select(LlmAdapterConfig).order_by(LlmAdapterConfig.id)
    ).all()]


def create_adapter(db: Session, body: dict) -> dict:
    protocol = str(body.get("protocol") or "openai-compatible").strip()
    row = LlmAdapterConfig(
        name=str(body.get("name") or body.get("code") or protocol).strip(),
        code=str(body.get("code") or "").strip(),
        protocol=protocol,
        api_base_url=str(body.get("api_base_url") or "").strip(),
        credential_id=int(body["credential_id"]) if body.get("credential_id") else None,
        settings_json=json.dumps(body.get("settings") or {}, ensure_ascii=False),
        is_active=bool(body.get("is_active", True)),
    )
    if not row.code:
        raise BizException.bad_request("缺少 adapter code")
    if row.credential_id and db.get(Credential, row.credential_id) is None:
        raise BizException.not_found("凭证")
    db.add(row)
    db.commit()
    db.refresh(row)
    return adapter_public(row)


def update_adapter(db: Session, adapter_id: int, body: dict) -> dict:
    row = db.get(LlmAdapterConfig, adapter_id)
    if row is None:
        raise BizException.not_found("Adapter")
    for field in ("name", "code", "protocol", "api_base_url"):
        if field in body and body[field] is not None:
            setattr(row, field, str(body[field]).strip())
    if "credential_id" in body:
        row.credential_id = int(body["credential_id"]) if body["credential_id"] else None
    if "settings" in body:
        row.settings_json = json.dumps(body.get("settings") or {}, ensure_ascii=False)
    if "is_active" in body:
        row.is_active = bool(body["is_active"])
    db.commit()
    db.refresh(row)
    return adapter_public(row)


def delete_adapter(db: Session, adapter_id: int) -> None:
    row = db.get(LlmAdapterConfig, adapter_id)
    if row is None:
        raise BizException.not_found("Adapter")
    in_use = db.scalar(
        select(LlmProvider.id).where(LlmProvider.adapter_id == adapter_id).limit(1)
    ) or db.scalar(select(LlmModel.id).where(LlmModel.adapter_id == adapter_id).limit(1))
    if in_use:
        raise BizException.bad_request("Adapter 正被厂商或模型引用")
    db.delete(row)
    db.commit()


def route_public(row: LlmTaskRoute) -> dict:
    return {
        "id": row.id,
        "task": row.task,
        "model_id": row.model_id,
        "adapter_id": row.adapter_id,
        "timeout_sec": row.timeout_sec,
        "retries": row.retries,
        "temperature": row.temperature,
        "reasoning": row.reasoning,
        "context_window": row.context_window,
        "fallback_model_ids": row.fallback_model_ids(),
        "is_active": bool(row.is_active),
    }


def list_routes(db: Session) -> list[dict]:
    return [route_public(row) for row in db.scalars(
        select(LlmTaskRoute).order_by(LlmTaskRoute.task)
    ).all()]


def _apply_route_body(row: LlmTaskRoute, body: dict) -> None:
    if "task" in body:
        task = str(body["task"]).strip()
        if task not in TASKS:
            raise BizException.bad_request(f"task 必须是 {', '.join(sorted(TASKS))}")
        row.task = task
    for field in ("model_id", "timeout_sec", "retries", "context_window"):
        if field in body and body[field] is not None:
            setattr(row, field, int(body[field]))
    if "adapter_id" in body:
        row.adapter_id = int(body["adapter_id"]) if body["adapter_id"] else None
    if "temperature" in body:
        row.temperature = float(body["temperature"]) if body["temperature"] is not None else None
    if "reasoning" in body:
        row.reasoning = bool(body["reasoning"]) if body["reasoning"] is not None else None
    if "fallback_model_ids" in body:
        values = [int(value) for value in (body.get("fallback_model_ids") or [])]
        row.fallback_model_ids_json = json.dumps(values)
    if "is_active" in body:
        row.is_active = bool(body["is_active"])


def create_route(db: Session, body: dict) -> dict:
    task = str(body.get("task") or "").strip()
    if task not in TASKS:
        raise BizException.bad_request(f"task 必须是 {', '.join(sorted(TASKS))}")
    model_id = int(body.get("model_id") or 0)
    if db.get(LlmModel, model_id) is None:
        raise BizException.not_found("模型")
    row = LlmTaskRoute(task=task, model_id=model_id)
    _apply_route_body(row, body)
    db.add(row)
    db.commit()
    db.refresh(row)
    return route_public(row)


def update_route(db: Session, route_id: int, body: dict) -> dict:
    row = db.get(LlmTaskRoute, route_id)
    if row is None:
        raise BizException.not_found("任务路由")
    _apply_route_body(row, body)
    if db.get(LlmModel, row.model_id) is None:
        raise BizException.not_found("模型")
    db.commit()
    db.refresh(row)
    return route_public(row)


def delete_route(db: Session, route_id: int) -> None:
    row = db.get(LlmTaskRoute, route_id)
    if row is None:
        raise BizException.not_found("任务路由")
    db.delete(row)
    db.commit()


@dataclass(frozen=True)
class RouteCandidate:
    task: str
    route: LlmTaskRoute | None
    model: LlmModel
    provider: LlmProvider
    adapter_config: LlmAdapterConfig | None
    fallback_from_model_id: int | None = None


def resolve_route(
    db: Session,
    task: str,
    *,
    model_pk: int | None = None,
) -> list[RouteCandidate]:
    if task not in TASKS:
        raise BizException.bad_request(f"未知 LLM task: {task}")
    route = db.scalar(
        select(LlmTaskRoute).where(
            LlmTaskRoute.task == task,
            LlmTaskRoute.is_active.is_(True),
        )
    )
    primary_id = model_pk or (route.model_id if route else None)
    primary, _ = resolve_invoke(db, primary_id)
    ids = [primary.id]
    if route and not model_pk:
        ids.extend(value for value in route.fallback_model_ids() if value not in ids)
    result: list[RouteCandidate] = []
    for index, candidate_id in enumerate(ids):
        model = db.get(LlmModel, candidate_id)
        if model is None or not model.is_active:
            continue
        provider = db.get(LlmProvider, model.provider_id)
        if provider is None or not provider.is_active:
            continue
        adapter_id = (route.adapter_id if route and index == 0 else None) or model.adapter_id or provider.adapter_id
        adapter = db.get(LlmAdapterConfig, adapter_id) if adapter_id else None
        result.append(RouteCandidate(
            task=task,
            route=route,
            model=model,
            provider=provider,
            adapter_config=adapter,
            fallback_from_model_id=primary.id if index else None,
        ))
    if not result:
        raise BizException.bad_request(f"任务 {task} 没有可用路由")
    return result


def _candidate_adapter(db: Session, candidate: RouteCandidate) -> LlmAdapter:
    route, model, provider, adapter = (
        candidate.route, candidate.model, candidate.provider, candidate.adapter_config
    )
    protocol = adapter.protocol if adapter else "openai-compatible"
    secret = (
        _credential_secret(db, adapter.credential_id)
        if adapter and adapter.credential_id
        else _provider_secret(db, provider, model)
    )
    config = LlmInvokeConfig(
        api_base_url=(adapter.api_base_url if adapter else "") or provider.api_base_url,
        api_key=secret,
        model=model.model_id,
        max_tokens=min(int(model.max_tokens or 2048), 8192),
        temperature=(
            float(route.temperature) if route and route.temperature is not None
            else float(model.temperature or 0.2)
        ),
        timeout_sec=route.timeout_sec if route else 90,
        reasoning=route.reasoning if route else None,
        context_window=(
            route.context_window if route and route.context_window
            else model.context_window or None
        ),
        extra=adapter.settings() if adapter else {},
    )
    return adapter_registry.create(protocol, config)


def record_invocation(
    db: Session,
    *,
    task: str,
    meta: dict | None = None,
    usage: dict | None = None,
    user_id: int | None = None,
    username: str = "",
    session_id: int | None = None,
    duration_ms: int = 0,
    status: str = "success",
    error_message: str = "",
    usage_source: str = "provider",
) -> None:
    """记下一次模型调用。助手主路径不走 invoke_task，必须自己记账，否则用量全丢。"""
    meta = meta or {}
    usage = usage or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    try:
        row = LlmObservation(
            task=task or "assistant",
            provider_id=int(meta["provider_id"]) if meta.get("provider_id") else None,
            provider_code=str(meta.get("provider_code") or ""),
            model_pk=int(meta["id"]) if meta.get("id") else None,
            model_id=str(meta.get("model_id") or ""),
            adapter_id=int(meta["adapter_id"]) if meta.get("adapter_id") else None,
            adapter_code=str(meta.get("adapter_code") or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            duration_ms=max(int(duration_ms or 0), 0),
            status=status or "success",
            error_message=(error_message or "")[:1000],
            finished_at=datetime.now(),
            user_id=int(user_id) if user_id else None,
            session_id=int(session_id) if session_id else None,
            username=(username or "")[:64],
            usage_source=usage_source if usage_source in {"provider", "estimated"} else "provider",
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("记录 LLM 用量失败")


def usage_tokens_from_turn(turn, messages: list, tools: list | None) -> tuple[dict, str]:
    """优先用厂商返回的 usage；没有就按启发式估，页面上也能看到消耗。"""
    from app.modules.ai.budget import estimate_tokens

    usage = dict(getattr(turn, "usage", None) or {}) if turn is not None else {}
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if inp > 0 or out > 0 or total > 0:
        if total <= 0:
            total = inp + out
        return {
            "prompt_tokens": inp,
            "completion_tokens": out,
            "total_tokens": total,
        }, "provider"
    inp = estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))
    if tools:
        inp += estimate_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    out = estimate_tokens(getattr(turn, "content", "") or "")
    out += estimate_tokens(json.dumps(getattr(turn, "tool_calls", None) or [], ensure_ascii=False, default=str))
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": inp + out,
    }, "estimated"


def usage_by_user(db: Session, *, days: int = 7) -> dict:
    """按用户汇总近期 token。管理员看谁在烧钱。"""
    from datetime import timedelta

    from sqlalchemy import func

    from app.modules.auth.models import User

    window = max(1, min(int(days or 7), 90))
    since = datetime.now() - timedelta(days=window)
    rows = db.execute(
        select(
            LlmObservation.user_id,
            LlmObservation.username,
            func.count(LlmObservation.id),
            func.coalesce(func.sum(LlmObservation.input_tokens), 0),
            func.coalesce(func.sum(LlmObservation.output_tokens), 0),
            func.coalesce(func.sum(LlmObservation.total_tokens), 0),
            func.max(LlmObservation.finished_at),
        )
        .where(
            LlmObservation.finished_at.is_not(None),
            LlmObservation.finished_at >= since,
        )
        .group_by(LlmObservation.user_id, LlmObservation.username)
        .order_by(func.coalesce(func.sum(LlmObservation.total_tokens), 0).desc())
    ).all()
    user_ids = [int(row[0]) for row in rows if row[0]]
    profiles = {}
    if user_ids:
        for user in db.scalars(select(User).where(User.id.in_(user_ids))).all():
            profiles[user.id] = user
    users = []
    requests = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for user_id, username, count, inp, out, total, last_used in rows:
        profile = profiles.get(int(user_id)) if user_id else None
        users.append(
            {
                "user_id": int(user_id) if user_id else 0,
                "username": (profile.username if profile else username) or "未知",
                "display_name": (profile.display_name if profile else "") or username or "未知",
                "requests": int(count or 0),
                "input_tokens": int(inp or 0),
                "output_tokens": int(out or 0),
                "total_tokens": int(total or 0),
                "last_used_at": last_used.isoformat() if last_used else "",
            }
        )
        requests += int(count or 0)
        input_tokens += int(inp or 0)
        output_tokens += int(out or 0)
        total_tokens += int(total or 0)
    return {
        "days": window,
        "since": since.isoformat(timespec="seconds"),
        "totals": {
            "users": len(users),
            "requests": requests,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "users": users,
    }


def _record_observation(
    db: Session,
    candidate: RouteCandidate,
    *,
    attempt: int,
    started: float,
    response: AdapterResponse | None = None,
    failure: LlmFailure | None = None,
) -> None:
    usage = response.usage if response else {}
    duration_ms = int((time.perf_counter() - started) * 1000)
    adapter = candidate.adapter_config
    row = LlmObservation(
        task=candidate.task,
        provider_id=candidate.provider.id,
        provider_code=candidate.provider.code,
        model_pk=candidate.model.id,
        model_id=candidate.model.model_id,
        adapter_id=adapter.id if adapter else None,
        adapter_code=adapter.code if adapter else "openai-compatible",
        request_id=(response.request_id if response else failure.request_id if failure else ""),
        attempt=attempt,
        fallback_from_model_id=candidate.fallback_from_model_id,
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        first_token_ms=response.first_token_ms if response else None,
        duration_ms=duration_ms,
        status="success" if response else "error",
        error_kind=failure.kind.value if failure else "",
        error_code=failure.code if failure else "",
        error_message=str(failure)[:1000] if failure else "",
        finished_at=datetime.now(),
    )
    db.add(row)
    db.commit()


def invoke_task(
    db: Session,
    task: str,
    messages: list[Message],
    *,
    tools: list[dict] | None = None,
    model_pk: int | None = None,
) -> tuple[AdapterResponse, dict]:
    """Invoke a routed task with retry/fallback and durable provenance."""

    last_failure: LlmFailure | None = None
    for candidate in resolve_route(db, task, model_pk=model_pk):
        retries = max(0, candidate.route.retries if candidate.route else 0)
        for attempt in range(1, retries + 2):
            started = time.perf_counter()
            try:
                response = _candidate_adapter(db, candidate).invoke(
                    messages,
                    tools=tools,
                    timeout_sec=candidate.route.timeout_sec if candidate.route else None,
                )
                _record_observation(
                    db, candidate, attempt=attempt, started=started, response=response,
                )
                return response, {
                    "task": task,
                    "provider_id": candidate.provider.id,
                    "provider_code": candidate.provider.code,
                    "model_pk": candidate.model.id,
                    "model_id": candidate.model.model_id,
                    "adapter_id": candidate.adapter_config.id if candidate.adapter_config else None,
                    "adapter_code": (
                        candidate.adapter_config.code
                        if candidate.adapter_config else "openai-compatible"
                    ),
                    "request_id": response.request_id,
                    "attempt": attempt,
                    "fallback_from_model_id": candidate.fallback_from_model_id,
                    "usage": response.usage,
                }
            except LlmFailure as failure:
                last_failure = failure
                _record_observation(
                    db, candidate, attempt=attempt, started=started, failure=failure,
                )
                if not failure.retryable:
                    break
            except Exception as exc:  # noqa: BLE001
                last_failure = LlmFailure(
                    FailureKind.PROVIDER, str(exc), code="unexpected_adapter_error",
                )
                _record_observation(
                    db, candidate, attempt=attempt, started=started, failure=last_failure,
                )
                break
    raise last_failure or LlmFailure(FailureKind.UNAVAILABLE, "没有可用 LLM 路由")


def observation_public(row: LlmObservation) -> dict:
    return {
        "id": row.id,
        "task": row.task,
        "provider_id": row.provider_id,
        "provider_code": row.provider_code,
        "model_pk": row.model_pk,
        "model_id": row.model_id,
        "adapter_id": row.adapter_id,
        "adapter_code": row.adapter_code,
        "request_id": row.request_id,
        "attempt": row.attempt,
        "fallback_from_model_id": row.fallback_from_model_id,
        "usage": {
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "total_tokens": row.total_tokens,
        },
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "total_tokens": row.total_tokens,
        "first_token_ms": row.first_token_ms,
        "duration_ms": row.duration_ms,
        "status": row.status,
        "error_kind": row.error_kind,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "username": row.username,
        "usage_source": getattr(row, "usage_source", "") or "provider",
    }


def list_observations(
    db: Session,
    *,
    task: str | None = None,
    status: str | None = None,
    request_id: str | None = None,
    user_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    statement = select(LlmObservation)
    if task:
        statement = statement.where(LlmObservation.task == task)
    if status:
        statement = statement.where(LlmObservation.status == status)
    if request_id:
        statement = statement.where(LlmObservation.request_id == request_id)
    if user_id:
        statement = statement.where(LlmObservation.user_id == int(user_id))
    rows = db.scalars(
        statement.order_by(LlmObservation.id.desc()).limit(max(1, min(limit, 500)))
    ).all()
    return [observation_public(row) for row in rows]


def get_observation(db: Session, observation_id: int) -> dict:
    row = db.get(LlmObservation, observation_id)
    if row is None:
        raise BizException.not_found("调用观测")
    return observation_public(row)


def delete_observation(db: Session, observation_id: int) -> None:
    row = db.get(LlmObservation, observation_id)
    if row is None:
        raise BizException.not_found("调用观测")
    db.delete(row)
    db.commit()
