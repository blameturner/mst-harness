"""Settings API — per-agent defaults and config.json override layer."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from infra import settings as _settings
from infra.config import PLATFORM, get_feature

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
