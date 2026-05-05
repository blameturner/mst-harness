"""NocoDB access layer for teaching tables.

All three tables (learner_curricula, learner_concepts, teaching_lessons) are
new — helpers use _safe_post/_safe_get/_safe_list so startup doesn't fail
before the tables are created.
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone

_log = logging.getLogger("teaching.db")


def get_learner_concepts(db, org_id: int, topic: str) -> list[dict]:
    return db._safe_list(
        "learner_concepts",
        f"(org_id,eq,{org_id})~and(topic,eq,{topic})",
        sort="-last_seen",
        limit=200,
    )


def upsert_learner_concept(db, org_id: int, topic: str, concept: str, mastery: str = "exposed") -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = db._safe_get(
        "learner_concepts",
        f"(org_id,eq,{org_id})~and(topic,eq,{topic})~and(concept,eq,{concept})",
    )
    if existing:
        db._patch("learner_concepts", int(existing["Id"]), {
            "mastery": mastery,
            "last_seen": now,
            "session_count": int(existing.get("session_count") or 0) + 1,
        })
    else:
        db._safe_post("learner_concepts", {
            "org_id": org_id,
            "topic": topic,
            "concept": concept,
            "mastery": mastery,
            "last_seen": now,
            "session_count": 1,
        })


def get_curriculum(db, curriculum_id: int) -> dict | None:
    return db._safe_get("learner_curricula", f"(Id,eq,{curriculum_id})")


def upsert_curriculum(
    db,
    org_id: int,
    topic: str,
    root_goal: str | None,
    modules: list[dict],
    curriculum_id: int | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    payload: dict = {
        "org_id": org_id,
        "topic": topic,
        "modules": _json.dumps(modules, ensure_ascii=False),
        "updated_at": now,
    }
    if root_goal:
        payload["root_goal"] = root_goal
    if curriculum_id:
        row = db._safe_get("learner_curricula", f"(Id,eq,{curriculum_id})")
        if row:
            return db._patch("learner_curricula", int(row["Id"]), payload) or row
    payload["current_module_index"] = 0
    return db._safe_post("learner_curricula", payload) or {}


def advance_curriculum_module(db, curriculum_id: int, module_id: str) -> None:
    row = db._safe_get("learner_curricula", f"(Id,eq,{curriculum_id})")
    if not row:
        return
    try:
        modules: list[dict] = _json.loads(row.get("modules") or "[]")
    except _json.JSONDecodeError:
        return
    for m in modules:
        if m.get("id") == module_id:
            m["status"] = "completed"
    current = int(row.get("current_module_index") or 0)
    db._patch("learner_curricula", int(row["Id"]), {
        "modules": _json.dumps(modules, ensure_ascii=False),
        "current_module_index": current + 1,
    })


def create_lesson_row(
    db,
    task_id: int,
    curriculum_id: int,
    module_id: str,
    lesson_markdown: str,
    anki_cards: str,
    session_summary: str,
    sources: list[dict],
    checks: list[dict],
) -> dict:
    return db._safe_post("teaching_lessons", {
        "task_id": task_id,
        "curriculum_id": curriculum_id,
        "module_id": module_id,
        "lesson_markdown": lesson_markdown,
        "anki_cards": anki_cards,
        "session_summary": session_summary,
        "sources": _json.dumps(sources, ensure_ascii=False),
        "checks": _json.dumps(checks, ensure_ascii=False),
    }) or {}


def update_lesson_row(
    db,
    lesson_id: int,
    lesson_markdown: str,
    anki_cards: str,
    session_summary: str,
    checks: list[dict],
    sources: list[dict],
) -> dict:
    return db._patch("teaching_lessons", lesson_id, {
        "lesson_markdown": lesson_markdown,
        "anki_cards": anki_cards,
        "session_summary": session_summary,
        "checks": _json.dumps(checks, ensure_ascii=False),
        "sources": _json.dumps(sources, ensure_ascii=False),
    })


def get_lesson_row(db, lesson_id: int) -> dict | None:
    return db._safe_get("teaching_lessons", f"(Id,eq,{lesson_id})")
