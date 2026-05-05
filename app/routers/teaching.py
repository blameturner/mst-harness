"""Teaching / adaptive-learning endpoints."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/teaching", tags=["teaching"])

CURRICULA_TABLE = "learner_curricula"
CONCEPTS_TABLE = "learner_concepts"
LESSONS_TABLE = "teaching_lessons"
TASK_TABLE = "task_list"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CurriculumCreate(BaseModel):
    topic: str
    org_id: int
    root_goal: str | None = None
    curriculum_id: int | None = None
    learner_note: str | None = None


class LessonCreate(BaseModel):
    topic: str
    org_id: int
    curriculum_id: int
    module_id: str
    learner_level: str | None = None


class ReviseRequest(BaseModel):
    revision_instructions: str


class CheckRequest(BaseModel):
    concept_focus: list[str] = []
    difficulty: str = "mixed"
    count: int = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(v: Any, default: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return v if v is not None else default


def _parse_task(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _row(row: dict) -> dict:
    payload = _parse_task(row.get("input_payload"))
    out: dict = {
        "id": str(row.get("Id", "")),
        "task_type": row.get("task_type", ""),
        "agent": row.get("agent") or "",
        "status": row.get("status", ""),
        "created_at": row.get("created_at") or row.get("CreatedAt", ""),
        "updated_at": row.get("updated_at") or row.get("UpdatedAt"),
        "input_payload": payload,
        "output_payload": _parse_task(row.get("output_payload")),
        "error": row.get("error"),
        "model": row.get("model"),
    }
    parent = row.get("parent_task_id") or (
        payload.get("parent_task_id") if isinstance(payload, dict) else None
    )
    if parent:
        out["parent_task_id"] = str(parent)
    return out


def _get_lesson_or_404(client: Any, lesson_id: str) -> dict:
    """Return teaching_lessons row or raise 404/502."""
    try:
        row = client._safe_get(LESSONS_TABLE, f"(Id,eq,{lesson_id})")
    except Exception as e:
        _log.error("lesson lookup error lesson_id=%s: %s", lesson_id, e)
        raise HTTPException(502, str(e))
    if not row:
        raise HTTPException(404, "lesson not found")
    return row


def _submit_task(client: Any, task_type: str, payload: dict, *, agent: str) -> dict:
    from workers import kanban
    try:
        task_id = kanban.submit(client, task_type, payload, created_by="api", agent=agent)
        resp = client._get(TASK_TABLE, params={"where": f"(Id,eq,{task_id})", "limit": 1})
        rows = resp.get("list", [])
        return _row(rows[0]) if rows else {"id": str(task_id), "status": "ready", "task_type": task_type}
    except Exception as e:
        _log.error("submit %s error: %s", task_type, e)
        raise HTTPException(502, str(e))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/topics")
def list_topics(org_id: int):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    try:
        rows = client._safe_list(CURRICULA_TABLE, f"(org_id,eq,{org_id})", sort="-Id")
    except Exception as e:
        _log.error("list_topics error org_id=%s: %s", org_id, e)
        raise HTTPException(502, str(e))

    topics = []
    for r in rows:
        modules = _parse_json(r.get("modules"), [])
        topics.append({
            "id": str(r.get("Id", "")),
            "topic": r.get("topic", ""),
            "root_goal": r.get("root_goal"),
            "module_count": len(modules),
            "completed_count": sum(1 for m in modules if m.get("status") == "completed"),
            "current_module_index": r.get("current_module_index"),
            "updated_at": r.get("updated_at") or r.get("UpdatedAt"),
        })
    return {"topics": topics}


@router.get("/curricula/{curriculum_id}")
def get_curriculum(curriculum_id: int):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    try:
        row = client._safe_get(CURRICULA_TABLE, f"(Id,eq,{curriculum_id})")
    except Exception as e:
        _log.error("get_curriculum error id=%s: %s", curriculum_id, e)
        raise HTTPException(502, str(e))
    if not row:
        raise HTTPException(404, "curriculum not found")
    return {
        "id": str(row.get("Id", "")),
        "topic": row.get("topic", ""),
        "org_id": row.get("org_id"),
        "root_goal": row.get("root_goal"),
        "modules": _parse_json(row.get("modules"), []),
        "current_module_index": row.get("current_module_index"),
        "updated_at": row.get("updated_at") or row.get("UpdatedAt"),
    }


@router.post("/curricula")
def create_curriculum(body: CurriculumCreate):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    payload = body.model_dump()
    return _submit_task(
        client,
        "teaching_curriculum",
        payload,
        agent=f"teaching:{body.org_id}",
    )


@router.get("/curricula/{curriculum_id}/lessons")
def list_lessons(curriculum_id: int):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    try:
        rows = client._safe_list(LESSONS_TABLE, f"(curriculum_id,eq,{curriculum_id})", sort="-Id")
    except Exception as e:
        _log.error("list_lessons error curriculum_id=%s: %s", curriculum_id, e)
        raise HTTPException(502, str(e))
    lessons = [
        {
            "id": str(r.get("Id", "")),
            "task_id": r.get("task_id"),
            "module_id": r.get("module_id"),
            "session_summary": r.get("session_summary"),
            "created_at": r.get("CreatedAt"),
        }
        for r in rows
    ]
    return {"lessons": lessons}


@router.get("/lessons/{lesson_id}")
def get_lesson(lesson_id: str):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    row = _get_lesson_or_404(client, lesson_id)
    return {
        "id": str(row.get("Id", "")),
        "task_id": row.get("task_id"),
        "curriculum_id": row.get("curriculum_id"),
        "module_id": row.get("module_id"),
        "lesson_markdown": row.get("lesson_markdown"),
        "anki_cards": row.get("anki_cards"),
        "session_summary": row.get("session_summary"),
        "sources": _parse_json(row.get("sources"), []),
        "checks": _parse_json(row.get("checks"), []),
        "created_at": row.get("CreatedAt"),
    }


@router.post("/lessons")
def create_lesson(body: LessonCreate):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    payload = body.model_dump()
    return _submit_task(
        client,
        "teaching_lesson",
        payload,
        agent=f"teaching:{body.org_id}",
    )


@router.post("/lessons/{lesson_id}/revise")
def revise_lesson(lesson_id: str, body: ReviseRequest):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    row = _get_lesson_or_404(client, lesson_id)
    task_id = row.get("task_id") or 0
    if not task_id:
        raise HTTPException(404, "lesson has no associated task")
    return _submit_task(
        client,
        "teaching_revision",
        {"parent_task_id": task_id, "revision_instructions": body.revision_instructions},
        agent=f"teaching:lesson:{lesson_id}",
    )


@router.post("/lessons/{lesson_id}/check")
def check_lesson(lesson_id: str, body: CheckRequest):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    row = _get_lesson_or_404(client, lesson_id)
    task_id = row.get("task_id") or 0
    if not task_id:
        raise HTTPException(404, "lesson has no associated task")
    return _submit_task(
        client,
        "teaching_check",
        {
            "parent_task_id": task_id,
            "concept_focus": body.concept_focus,
            "difficulty": body.difficulty,
            "count": body.count,
        },
        agent=f"teaching:lesson:{lesson_id}",
    )


@router.get("/learner")
def get_learner_concepts(org_id: int, topic: str):
    from infra.nocodb_client import NocodbClient
    client = NocodbClient()
    where = f"(org_id,eq,{org_id})~and(topic,eq,{topic})"
    try:
        rows = client._safe_list(CONCEPTS_TABLE, where, sort="-Id")
    except Exception as e:
        _log.error("get_learner_concepts error org_id=%s topic=%s: %s", org_id, topic, e)
        raise HTTPException(502, str(e))
    concepts = [
        {
            "id": str(r.get("Id", "")),
            "concept": r.get("concept"),
            "mastery": r.get("mastery"),
            "last_seen": r.get("last_seen"),
            "session_count": r.get("session_count"),
            "misconceptions": r.get("misconceptions"),
            "preferred_style": r.get("preferred_style"),
        }
        for r in rows
    ]
    return {"concepts": concepts}
