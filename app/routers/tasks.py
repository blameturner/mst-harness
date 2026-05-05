"""CRUD API for the kanban task_list table."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from infra.nocodb_client import NocodbClient
from workers.task_handlers.project_feature import _DEFAULT_HUMAN_REVIEW_THRESHOLD

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

TASK_TABLE = "task_list"


class AutonomySettings(BaseModel):
    autonomy_mode: str | None = None
    max_tasks_per_hour: int | None = None
    max_queued_proposals: int | None = None
    max_daily_tokens: int | None = None
    consecutive_failure_backoff: bool | None = None
    model_agent: str | None = None
    model_po: str | None = None
    staging_branch: str | None = None
    architectural_rules: str | None = None
    human_review_threshold_files: int | None = None


class TriggerFeatureRequest(BaseModel):
    feature_description: str
    branch_name: str
    architect_context: str | None = None
    model: str | None = None


class TriggerProposeRequest(BaseModel):
    model: str | None = None


class ApproveHumanReviewRequest(BaseModel):
    feedback: str | None = None


class TaskCreate(BaseModel):
    task_type: str
    agent: str = ""
    input_payload: Any = None
    model: str | None = None
    prompt_template_id: str | None = None


def _parse(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _row(row: dict) -> dict:
    payload = _parse(row.get("input_payload"))
    out: dict = {
        "id": str(row.get("Id", "")),
        "task_type": row.get("task_type", ""),
        "agent": row.get("agent") or "",
        "status": row.get("status", ""),
        "created_at": row.get("created_at") or row.get("CreatedAt", ""),
        "updated_at": row.get("updated_at") or row.get("UpdatedAt"),
        "input_payload": payload,
        "output_payload": _parse(row.get("output_payload")),
        "error": row.get("error"),
        "model": row.get("model"),
        "prompt_template_id": row.get("prompt_template_id"),
    }
    # Surface parent_task_id for research_revision tasks
    parent = row.get("parent_task_id") or (
        payload.get("parent_task_id") if isinstance(payload, dict) else None
    )
    if parent:
        out["parent_task_id"] = str(parent)
    return out


@router.get("")
def list_tasks(status: str | None = None, task_type: str | None = None):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    parts: list[str] = []
    if status:
        parts.append(f"(status,eq,{status})")
    if task_type:
        parts.append(f"(task_type,eq,{task_type})")
    params: dict = {"limit": 200, "sort": "-Id"}
    if parts:
        params["where"] = "~and".join(parts)
    try:
        resp = client._get(TASK_TABLE, params=params)
        return {"tasks": [_row(r) for r in resp.get("list", [])]}
    except Exception as e:
        _log.error("list_tasks error: %s", e)
        raise HTTPException(502, str(e))


@router.get("/{task_id}")
def get_task(task_id: str):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    try:
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        if not rows:
            raise HTTPException(404, "task not found")
        task = _row(rows[0])
        # Fetch revisions: research_revision tasks whose input_payload.parent_task_id matches
        rev_resp = client._get(TASK_TABLE, params={
            "where": "(task_type,eq,research_revision)",
            "limit": 100,
            "sort": "-Id",
        })
        task["children"] = [
            _row(r) for r in rev_resp.get("list", [])
            if str(_parse(r.get("input_payload") or {}).get("parent_task_id", "")) == task_id
        ]
        return task
    except HTTPException:
        raise
    except Exception as e:
        _log.error("get_task error task_id=%s: %s", task_id, e)
        raise HTTPException(502, str(e))


@router.post("")
def create_task(body: TaskCreate):
    from infra.nocodb_client import NocodbClient
    from workers import kanban
    client = NocodbClient()
    payload = body.input_payload if isinstance(body.input_payload, dict) else {}
    try:
        task_id = kanban.submit(
            client,
            body.task_type,
            payload,
            created_by="api",
            agent=body.agent or "",
        )
        patch: dict = {}
        if body.model:
            patch["model"] = body.model
        if body.prompt_template_id:
            patch["prompt_template_id"] = body.prompt_template_id
        if patch:
            client._patch(TASK_TABLE, task_id, patch)
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": body.task_type}
    except Exception as e:
        _log.error("create_task error: %s", e)
        raise HTTPException(502, str(e))


@router.get("/projects/{project_id}/autonomy")
def get_autonomy(project_id: int):
    from infra.settings import get_agent_setting
    from infra.config import get_feature
    from workers.project_autonomy import _DEFAULTS

    agent = f"project:{project_id}"
    result: dict = {}
    for key, default in _DEFAULTS.items():
        db_val = get_agent_setting(agent, key)
        result[key] = db_val if db_val is not None else get_feature("project", key, default)
    _EXTENDED_DEFAULTS = {
        "model_agent":                    (get_feature("project", "models") or {}).get("project_agent", {}).get("role", "t2_coder"),
        "model_po":                       (get_feature("project", "models") or {}).get("project_po",    {}).get("role", "t1_primary"),
        "staging_branch":                 "staging",
        "architectural_rules":            "",
        "human_review_threshold_files":   _DEFAULT_HUMAN_REVIEW_THRESHOLD,
    }
    for key, default in _EXTENDED_DEFAULTS.items():
        db_val = get_agent_setting(agent, key)
        result[key] = db_val if db_val is not None else default
    result["_halted"] = bool(get_agent_setting(agent, "_halted"))
    return result


@router.put("/projects/{project_id}/autonomy")
def put_autonomy(project_id: int, body: AutonomySettings):
    from infra.settings import set_agent_setting

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "no fields provided")
    agent = f"project:{project_id}"
    for key, value in updates.items():
        set_agent_setting(agent, key, value)
    return {"updated": list(updates.keys())}


@router.post("/projects/{project_id}/autonomy/resume")
def resume_autonomy(project_id: int):
    """Clear a project halt so autonomy tasks can run again."""
    from infra.settings import set_agent_setting

    set_agent_setting(f"project:{project_id}", "_halted", False)
    return {"resumed": True}


@router.post("/projects/{project_id}/human-reviews/{task_id}/approve")
def approve_human_review(project_id: int, task_id: int, body: ApproveHumanReviewRequest):
    """Approve a blocked project_human_review task and optionally provide feedback."""
    client = NocodbClient()
    resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
    rows = resp.get("list", [])
    if not rows:
        raise HTTPException(404, "task not found")
    row = rows[0]
    if row.get("task_type") != "project_human_review":
        raise HTTPException(400, "task is not a project_human_review task")
    if row.get("status") != "blocked":
        raise HTTPException(409, "task is not awaiting review")

    payload = _parse(row.get("input_payload")) or {}
    if body.feedback:
        payload["human_feedback"] = body.feedback
    patch: dict = {
        "status": "ready",
        "input_payload": json.dumps(payload),
    }
    client._patch(TASK_TABLE, task_id, patch)
    return {"approved": True, "task_id": task_id, "has_feedback": bool(body.feedback)}


@router.post("/projects/{project_id}/trigger/feature")
def trigger_feature(project_id: int, body: TriggerFeatureRequest):
    """Manually queue a project_feature task. Always creates status='ready'."""
    from infra.nocodb_client import NocodbClient
    from workers import kanban
    client = NocodbClient()
    payload = {
        "project_id": project_id,
        "feature_description": body.feature_description,
        "branch_name": body.branch_name,
        "architect_context": body.architect_context,
        "revision_count": 0,
    }
    try:
        task_id = kanban.submit(
            client,
            "project_feature",
            payload,
            status="ready",
            created_by="api",
            agent=f"project:{project_id}",
            model=body.model or "",
        )
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": "project_feature"}
    except Exception as e:
        _log.error("trigger_feature error: %s", e)
        raise HTTPException(502, str(e))


@router.post("/projects/{project_id}/trigger/propose")
def trigger_propose(project_id: int, body: TriggerProposeRequest):
    """Manually queue a project_propose task."""
    from infra.nocodb_client import NocodbClient
    from workers import kanban
    client = NocodbClient()
    payload = {"project_id": project_id}
    try:
        task_id = kanban.submit(
            client,
            "project_propose",
            payload,
            status="ready",
            created_by="api",
            agent=f"project:{project_id}",
            model=body.model or "",
        )
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": "project_propose"}
    except Exception as e:
        _log.error("trigger_propose error: %s", e)
        raise HTTPException(502, str(e))
