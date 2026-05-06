"""Per-agent and system-wide settings stored in NocoDB.

Lookup order for get_agent_setting(agent, key):
  1. agent row in settings table
  2. __system__ row
  3. None  (caller falls back to config.json)

Config.json overrides (all feature flags / tuning knobs) live in the __config__
row, keyed as {section: {key: value}}. Use get_feature_with_override() instead
of get_feature() at call sites that should respect DB overrides.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

_TABLE = "settings"
SYSTEM_AGENT = "__system__"
CONFIG_AGENT = "__config__"

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ── NocoDB helpers ─────────────────────────────────────────────────────────────

def _db():
    from infra.nocodb_client import NocodbClient
    return NocodbClient()


def _table_exists() -> bool:
    try:
        db = _db()
        return db._has_table(_TABLE)
    except Exception:
        return False


def _fetch_row(agent: str) -> dict:
    """Return the data dict for agent, or {} if missing/table absent."""
    try:
        db = _db()
        if not db._has_table(_TABLE):
            _log.warning("settings table not found; all settings will return None")
            return {}
        rows = db._get(_TABLE, {"where": f"(agent,eq,{agent})", "limit": 1}).get("list", [])
        if not rows:
            return {}
        raw = rows[0].get("data") or "{}"
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        _log.warning("settings fetch failed for agent=%s", agent, exc_info=True)
        return {}


def _write_row(agent: str, data: dict) -> None:
    try:
        db = _db()
        if not db._has_table(_TABLE):
            return
        rows = db._get(_TABLE, {"where": f"(agent,eq,{agent})", "limit": 1}).get("list", [])
        payload = {"agent": agent, "data": json.dumps(data)}
        if rows:
            db._patch(_TABLE, rows[0]["Id"], {"data": json.dumps(data)})
        else:
            db._post(_TABLE, payload)
    except Exception:
        _log.error("settings write failed for agent=%s", agent, exc_info=True)


# ── cache ──────────────────────────────────────────────────────────────────────

def _cached(agent: str) -> dict:
    with _cache_lock:
        if agent in _cache:
            return _cache[agent]
    data = _fetch_row(agent)
    with _cache_lock:
        _cache[agent] = data
    return data


def _invalidate(agent: str) -> None:
    with _cache_lock:
        _cache.pop(agent, None)


# ── public API ─────────────────────────────────────────────────────────────────

def get_agent_setting(agent: str, key: str) -> Any:
    val = _cached(agent).get(key)
    if val is not None:
        return val
    return _cached(SYSTEM_AGENT).get(key)


def set_agent_setting(agent: str, key: str, value: Any) -> None:
    data = dict(_cached(agent))
    data[key] = value
    _write_row(agent, data)
    _invalidate(agent)


def get_system_setting(key: str) -> Any:
    return _cached(SYSTEM_AGENT).get(key)


def set_system_setting(key: str, value: Any) -> None:
    set_agent_setting(SYSTEM_AGENT, key, value)


def get_all_settings() -> dict:
    """Return every row for the UI. Excludes the config-overrides row."""
    try:
        db = _db()
        if not db._has_table(_TABLE):
            return {"agents": {}, "system": {}}
        rows = db._get(_TABLE, {"limit": 500}).get("list", [])
        agents: dict[str, dict] = {}
        system: dict = {}
        for row in rows:
            agent = row.get("agent", "")
            raw = row.get("data") or "{}"
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if agent == SYSTEM_AGENT:
                system = data
            elif agent not in (CONFIG_AGENT, OPENROUTER_AGENT):
                agents[agent] = data
        return {"agents": agents, "system": system}
    except Exception:
        _log.warning("get_all_settings failed", exc_info=True)
        return {"agents": {}, "system": {}}


# ── config.json override layer ─────────────────────────────────────────────────

def get_config_override(section: str, key: str) -> Any:
    """Check if a config.json feature value has been overridden in the DB."""
    overrides = _cached(CONFIG_AGENT)
    section_data = overrides.get(section)
    if not isinstance(section_data, dict):
        return None
    return section_data.get(key)


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for k, v in patch.items():
        result[k] = _deep_merge(result[k], v) if isinstance(v, dict) and isinstance(result.get(k), dict) else v
    return result


def set_config_override(section: str, key: str, value: Any) -> None:
    overrides = dict(_cached(CONFIG_AGENT))
    section_data = dict(overrides.get(section) or {})
    existing = section_data.get(key)
    section_data[key] = _deep_merge(existing, value) if isinstance(value, dict) and isinstance(existing, dict) else value
    overrides[section] = section_data
    _write_row(CONFIG_AGENT, overrides)
    _invalidate(CONFIG_AGENT)


def get_config_overrides_all() -> dict:
    return dict(_cached(CONFIG_AGENT))


def get_feature_with_override(section: str, key: str, default: Any = None) -> Any:
    """get_feature() with DB override precedence."""
    override = get_config_override(section, key)
    if override is not None:
        return override
    from infra.config import get_feature
    return get_feature(section, key, default)


# ── OpenRouter connection ──────────────────────────────────────────────────────

OPENROUTER_AGENT = "__openrouter__"


def get_openrouter_connection() -> dict | None:
    """Return stored OpenRouter config, or None if no API key is set."""
    data = _cached(OPENROUTER_AGENT)
    return data if data.get("api_key") else None


def upsert_openrouter_connection(api_key: str, base_url: str = "") -> dict:
    # Preserve extra fields (e.g. allowed_models) set independently.
    existing = dict(_cached(OPENROUTER_AGENT))
    data: dict = {**existing, "api_key": api_key, "base_url": base_url}
    _write_row(OPENROUTER_AGENT, data)
    _invalidate(OPENROUTER_AGENT)
    return data


def delete_openrouter_connection() -> bool:
    data = _cached(OPENROUTER_AGENT)
    if not data.get("api_key"):
        return False
    _write_row(OPENROUTER_AGENT, {})
    _invalidate(OPENROUTER_AGENT)
    return True


def mark_openrouter_verified() -> None:
    data = dict(_cached(OPENROUTER_AGENT))
    if not data:
        return
    data["verified_at"] = datetime.now(timezone.utc).isoformat()
    _write_row(OPENROUTER_AGENT, data)
    _invalidate(OPENROUTER_AGENT)
