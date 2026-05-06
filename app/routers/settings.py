"""Settings API — per-agent defaults, config.json overrides, and external connections."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from infra import settings as _settings
from infra.config import PLATFORM, get_feature
from shared.model_client import reset_model_client

_log = logging.getLogger("main.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


class AgentSettingsPayload(BaseModel):
    model: str | None = None
    max_tokens_per_task: int | None = None
    max_tasks_per_hour: int | None = None
    max_daily_tokens: int | None = None


class SystemSettingsPayload(BaseModel):
    fallback_model: str | None = None


class ConfigOverridePayload(BaseModel):
    values: dict[str, Any]


@router.get("")
def get_all():
    return _settings.get_all_settings()


@router.get("/agent/{agent}")
def get_agent(agent: str):
    from infra.settings import _cached
    return _cached(agent)


@router.patch("/agent/{agent}")
def patch_agent(agent: str, payload: AgentSettingsPayload):
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields provided")
    for key, value in updates.items():
        _settings.set_agent_setting(agent, key, value)
    return {"ok": True, "agent": agent, "updated": list(updates.keys())}


@router.get("/system")
def get_system():
    return _settings.get_all_settings()["system"]


@router.patch("/system")
def patch_system(payload: SystemSettingsPayload):
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields provided")
    for key, value in updates.items():
        _settings.set_system_setting(key, value)
    return {"ok": True, "updated": list(updates.keys())}


@router.get("/config")
def get_config():
    """Config.json defaults merged with DB overrides. Overrides marked explicitly."""
    features = PLATFORM.get("features", {})
    overrides = _settings.get_config_overrides_all()
    result: dict[str, Any] = {}
    for section, section_data in features.items():
        if not isinstance(section_data, dict):
            result[section] = {"_value": section_data, "_overrides": {}}
            continue
        section_overrides = overrides.get(section) or {}
        merged = {**section_data, **section_overrides}
        result[section] = {"_defaults": section_data, "_overrides": section_overrides, "_merged": merged}
    return result


@router.patch("/config/{section}")
def patch_config_section(section: str, payload: ConfigOverridePayload):
    features = PLATFORM.get("features", {})
    if section not in features:
        raise HTTPException(404, f"Section '{section}' not found in config.json")
    for key, value in payload.values.items():
        _settings.set_config_override(section, key, value)
    # Apply any model-level overrides to the live PLATFORM registry so
    # get_function_config() picks them up without a server restart.
    section_overrides = _settings.get_config_overrides_all().get(section) or {}
    nested_models = section_overrides.get("models")
    if isinstance(nested_models, dict):
        live: dict = PLATFORM.setdefault("models", {})
        for fn_name, fn_cfg in nested_models.items():
            if isinstance(fn_cfg, dict):
                live[fn_name] = {**(live.get(fn_name) or {}), **fn_cfg}
    return {"ok": True, "section": section, "updated": list(payload.values.keys())}


@router.delete("/config/{section}/{key}")
def delete_config_override(section: str, key: str):
    """Remove a single config override, reverting that key to config.json default."""
    overrides = dict(_settings.get_config_overrides_all())
    section_data = dict(overrides.get(section) or {})
    if key not in section_data:
        raise HTTPException(404, f"No override for {section}.{key}")
    del section_data[key]
    overrides[section] = section_data
    from infra.settings import _write_row, _invalidate, CONFIG_AGENT
    _write_row(CONFIG_AGENT, overrides)
    _invalidate(CONFIG_AGENT)
    return {"ok": True, "section": section, "key": key}


# ── OpenRouter connection ──────────────────────────────────────────────────────

_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Private IP ranges that must never be targeted by user-supplied base_url (SSRF).
_SSRF_BLOCKED_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "::1", "0.", "localhost")


class OpenRouterConnectionPayload(BaseModel):
    api_key: str
    base_url: str = ""


def _validate_base_url(url: str) -> str:
    """Reject non-HTTPS and private-range targets to prevent SSRF."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(400, "base_url must use https://")
    host = parsed.hostname or ""
    if any(host.startswith(p) for p in _SSRF_BLOCKED_PREFIXES):
        raise HTTPException(400, "base_url must not target a private network address")
    return url.rstrip("/")


def _redact_openrouter(conn: dict | None) -> dict | None:
    if not conn:
        return None
    out = dict(conn)
    if out.get("api_key"):
        out["api_key"] = "***"
    return out


@router.get("/connections/openrouter")
def get_openrouter_connection():
    return {"connection": _redact_openrouter(_settings.get_openrouter_connection())}


@router.put("/connections/openrouter")
async def upsert_openrouter_connection(body: OpenRouterConnectionPayload):
    safe_base_url = _validate_base_url(body.base_url)
    conn = _settings.upsert_openrouter_connection(body.api_key, safe_base_url)
    effective_url = safe_base_url or _OPENROUTER_DEFAULT_BASE_URL
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{effective_url}/models",
                headers={"Authorization": f"Bearer {body.api_key}"},
            )
            r.raise_for_status()
        _settings.mark_openrouter_verified()
        verified = True
    except Exception as exc:
        _log.warning("openrouter verify failed: %s", exc)
        verified = False
    reset_model_client()
    return {"connection": _redact_openrouter(conn), "verified": verified}


@router.delete("/connections/openrouter")
def delete_openrouter_connection():
    ok = _settings.delete_openrouter_connection()
    if ok:
        reset_model_client()
    return {"ok": ok}


@router.get("/connections/openrouter/models")
async def list_openrouter_models():
    """Return the list of models available on the configured OpenRouter account."""
    conn = _settings.get_openrouter_connection()
    if not conn or not conn.get("api_key"):
        return {"models": [], "enabled": False}
    base_url = conn.get("base_url") or _OPENROUTER_DEFAULT_BASE_URL
    allowed = conn.get("allowed_models") or []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {conn['api_key']}"},
            )
            r.raise_for_status()
            data = r.json().get("data") or []
            models = [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
            return {"models": models, "allowed": allowed, "enabled": True}
    except Exception as exc:
        _log.warning("openrouter models fetch failed: %s", exc)
        fallback: list[str] = []
        return {"models": fallback, "allowed": allowed, "enabled": True, "fallback": True}


class OpenRouterAllowlistPayload(BaseModel):
    models: list[str]


@router.patch("/connections/openrouter/allowlist")
def set_openrouter_allowlist(payload: OpenRouterAllowlistPayload):
    _settings.set_agent_setting(_settings.OPENROUTER_AGENT, "allowed_models", payload.models)
    reset_model_client()
    return {"ok": True, "count": len(payload.models)}


@router.post("/connections/openrouter/test")
async def test_openrouter_connection():
    conn = _settings.get_openrouter_connection()
    if not conn or not conn.get("api_key"):
        raise HTTPException(400, "openrouter not configured")
    base_url = conn.get("base_url") or _OPENROUTER_DEFAULT_BASE_URL
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {conn['api_key']}"},
            )
            r.raise_for_status()
            model_count = len(r.json().get("data") or [])
        _settings.mark_openrouter_verified()
        return {"ok": True, "model_count": model_count}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": str(exc), "status": exc.response.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
