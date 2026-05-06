"""Kanban handler for 'teaching_lesson' tasks.

Two-phase execution to avoid monopolising the LLM loop:

  Phase 1 (no research_plan_id in payload)
    - Validate the curriculum and module exist
    - Create a research plan (defer_run=True) for the module topic
    - Submit research_planner to kanban (it auto-queues research_agent on completion)
    - Patch this task's input_payload with research_plan_id so phase 2 can find it
    - Raise TaskNotReady — the LLM loop is freed while research runs

  Phase 2 (research_plan_id present)
    - Re-check the research plan status; wait if still in progress
    - Generate the lesson from the finished paper

Input:  {topic, org_id, curriculum_id, module_id, learner_level?}
Output: {lesson_id, lesson_markdown, session_summary, anki_cards, checks, sources, paths}
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler, TaskNotReady

_log = logging.getLogger("teaching.lesson")

# Plan statuses that mean "not done yet — check back later"
_RESEARCH_IN_PROGRESS = frozenset({"pending", "generating", "searching", "synthesizing", "queued"})


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    curriculum_id = int(payload.get("curriculum_id") or 0)
    module_id = (payload.get("module_id") or "").strip()
    task_id = int(task.get("Id") or 0)

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}
    if not org_id:
        return {"status": "failed", "error": "input_payload.org_id is required"}
    if not curriculum_id:
        return {"status": "failed", "error": "input_payload.curriculum_id is required"}
    if not module_id:
        return {"status": "failed", "error": "input_payload.module_id is required"}

    if payload.get("research_plan_id"):
        return _lesson_phase(task_id, payload)
    return _research_phase(task_id, payload, topic, org_id, curriculum_id, module_id)


def _research_phase(
    task_id: int, payload: dict,
    topic: str, org_id: int, curriculum_id: int, module_id: str,
) -> dict:
    """Phase 1: validate inputs, create a research plan, queue it, then wait."""
    try:
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import get_curriculum
        from tools.research.research_planner import create_research_plan
        from workers import kanban

        db = NocodbClient()

        curriculum = get_curriculum(db, curriculum_id)
        if not curriculum:
            return {"status": "failed", "error": f"curriculum {curriculum_id} not found"}

        try:
            modules: list[dict] = _json.loads(curriculum.get("modules") or "[]")
        except _json.JSONDecodeError:
            modules = []
        target = next((m for m in modules if m.get("id") == module_id), None)
        if not target:
            return {"status": "failed", "error": f"module {module_id!r} not found in curriculum {curriculum_id}"}

        module_title: str = target.get("title") or module_id
        research_query = f"{topic}: {module_title}"
        plan_result = create_research_plan(topic=research_query, org_id=org_id, defer_run=True)
        if plan_result.get("status") not in ("deferred", "pending"):
            return {"status": "failed", "error": f"research plan creation failed: {plan_result.get('error', plan_result.get('status'))}"}
        plan_id = int(plan_result.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": "research plan creation returned no plan_id"}

        # research_planner auto-queues research_agent on completion (queue_agent=True default)
        kanban.submit(
            db, "research_planner", {"plan_id": plan_id},
            created_by="teaching_lesson", agent=f"teaching:{org_id}",
        )

        # Record plan_id so phase 2 can find the finished research
        db._patch("task_list", task_id, {"input_payload": {**payload, "research_plan_id": plan_id}})
        _log.info("teaching_lesson research_phase  task_id=%d plan_id=%d", task_id, plan_id)
        raise TaskNotReady(f"research plan {plan_id} submitted", delay_seconds=90)

    except TaskNotReady:
        raise
    except Exception as exc:
        _log.error("teaching_lesson research_phase  task_id=%d err=%s", task_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400]}


def _lesson_phase(task_id: int, payload: dict) -> dict:
    """Phase 2: research is complete — generate the lesson."""
    research_plan_id = int(payload.get("research_plan_id") or 0)
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    curriculum_id = int(payload.get("curriculum_id") or 0)
    module_id = (payload.get("module_id") or "").strip()
    learner_level = (payload.get("learner_level") or "").strip() or None

    try:
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import (
            get_curriculum, get_learner_concepts,
            create_lesson_row, advance_curriculum_module, upsert_learner_concept,
        )
        from tools.teaching.llm import generate_lesson
        from tools.teaching.output import write_lesson_files

        db = NocodbClient()

        plan_rows = db._get(
            "research_plans",
            params={"where": f"(Id,eq,{research_plan_id})", "limit": 1},
        ).get("list", [])
        if not plan_rows:
            return {"status": "failed", "error": f"research_plans row {research_plan_id} not found"}

        plan = plan_rows[0]
        plan_status = (plan.get("status") or "").strip()
        if plan_status in ("failed", "error"):
            return {"status": "failed", "error": f"research plan {research_plan_id} failed: {plan.get('error_message', '')[:200]}"}
        if plan_status != "completed":
            raise TaskNotReady(f"research plan {research_plan_id} status={plan_status!r}", delay_seconds=120)

        curriculum = get_curriculum(db, curriculum_id)
        if not curriculum:
            return {"status": "failed", "error": f"curriculum {curriculum_id} not found"}

        try:
            modules: list[dict] = _json.loads(curriculum.get("modules") or "[]")
        except _json.JSONDecodeError:
            modules = []
        target = next((m for m in modules if m.get("id") == module_id), None)
        if not target:
            return {"status": "failed", "error": f"module {module_id!r} not found in curriculum {curriculum_id}"}

        objectives: list[str] = target.get("objectives") or []
        module_title: str = target.get("title") or module_id
        known_concepts = get_learner_concepts(db, org_id, topic)
        research_text = (plan.get("paper_content") or "").strip()

        lesson_markdown, session_summary, anki_cards, checks = generate_lesson(
            topic=topic,
            module_title=module_title,
            objectives=objectives,
            learner_level=learner_level or _infer_level(known_concepts),
            known_concepts=known_concepts,
            research_text=research_text,
        )

        lesson_path, cards_path = write_lesson_files(task_id, lesson_markdown, anki_cards)
        lesson_row = create_lesson_row(
            db, task_id, curriculum_id, module_id,
            lesson_markdown, anki_cards, session_summary, [], checks,
        )
        lesson_id = int(lesson_row.get("Id") or 0)

        advance_curriculum_module(db, curriculum_id, module_id)
        for obj in objectives:
            upsert_learner_concept(db, org_id, topic, obj, mastery="exposed")

        _log.info("teaching_lesson done  org=%d topic=%r module=%s lesson_id=%d", org_id, topic, module_id, lesson_id)
        return {
            "status": "completed",
            "lesson_id": lesson_id,
            "curriculum_id": curriculum_id,
            "module_id": module_id,
            "lesson_markdown": lesson_markdown,
            "session_summary": session_summary,
            "anki_cards": anki_cards,
            "checks": checks,
            "sources": [],
            "lesson_path": lesson_path,
            "cards_path": cards_path,
        }

    except TaskNotReady:
        raise
    except Exception as exc:
        _log.error("teaching_lesson lesson_phase  task_id=%d plan_id=%d err=%s",
                   task_id, research_plan_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400]}


def _infer_level(concepts: list[dict]) -> str:
    if not concepts:
        return "beginner"
    verified = sum(1 for c in concepts if c.get("mastery") == "verified")
    practiced = sum(1 for c in concepts if c.get("mastery") == "practiced")
    if verified >= 5 or practiced >= 10:
        return "advanced"
    if verified >= 2 or practiced >= 3:
        return "intermediate"
    return "beginner"


_type_check: TaskHandler = handle
