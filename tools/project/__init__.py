"""Project agent tooling."""
from __future__ import annotations


def resolve_agent_model(task: dict, config_key: str) -> str:
    """Resolve model role for a project task.

    Precedence: task.model column → features.project.models.<config_key>.role → hardcoded fallback.
    """
    override = str(task.get("model") or "").strip()
    if override:
        return override
    from infra.config import get_feature
    entry = (get_feature("project", "models") or {}).get(config_key) or {}
    defaults = {"project_agent": "t2_coder", "project_po": "t1_primary"}
    return str(entry.get("role") or defaults.get(config_key, "t2_coder"))
