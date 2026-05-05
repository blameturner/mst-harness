"""Kanban handler for 'teaching_curriculum' tasks.

Builds or updates the learning curriculum for a topic. Uses the learner model
to calibrate depth and sequencing. No search; runs from model knowledge.
Input: {topic, org_id, root_goal?, curriculum_id?, learner_note?}.
Output: {curriculum_id, module_count, modules}.
"""
from __future__ import annotations
import asyncio
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.curriculum")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    root_goal = (payload.get("root_goal") or "").strip() or None
    curriculum_id = int(payload.get("curriculum_id") or 0) or None
    learner_note = (payload.get("learner_note") or "").strip() or None

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}
    if not org_id:
        return {"status": "failed", "error": "input_payload.org_id is required"}

    try:
        import json as _json
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import get_learner_concepts, get_curriculum, upsert_curriculum
        from tools.teaching.llm import generate_curriculum_modules

        db = NocodbClient()
        known_concepts = get_learner_concepts(db, org_id, topic)

        existing_modules: list[dict] | None = None
        if curriculum_id:
            row = get_curriculum(db, curriculum_id)
            if row:
                try:
                    existing_modules = _json.loads(row.get("modules") or "[]")
                except _json.JSONDecodeError:
                    existing_modules = None

        modules = generate_curriculum_modules(
            topic=topic,
            root_goal=root_goal,
            learner_note=learner_note,
            known_concepts=known_concepts,
            existing_modules=existing_modules,
        )

        row = upsert_curriculum(db, org_id, topic, root_goal, modules, curriculum_id)
        row_id = int(row.get("Id") or 0)
        if not row_id:
            return {"status": "failed", "error": "curriculum upsert did not return a row Id — table may not exist"}

        _log.info("teaching_curriculum done  org=%d topic=%r curriculum_id=%d modules=%d", org_id, topic, row_id, len(modules))
        return {
            "status": "completed",
            "curriculum_id": row_id,
            "topic": topic,
            "module_count": len(modules),
            "modules": modules,
        }

    except Exception as exc:
        _log.error("teaching_curriculum uncaught  org=%d topic=%r err=%s", org_id, topic[:80], exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400]}


_type_check: TaskHandler = handle
