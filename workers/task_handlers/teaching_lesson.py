"""Kanban handler for 'teaching_lesson' tasks.

Runs a planned search pipeline to source-ground the lesson, then produces
a full depth lesson for one curriculum module: prose, examples, checks,
Anki cards, and session summary. Updates curriculum and learner_concepts.
Input: {topic, org_id, curriculum_id, module_id, learner_level?}.
Output: {lesson_id, lesson_markdown, session_summary, anki_cards, checks, sources, paths}.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.lesson")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    curriculum_id = int(payload.get("curriculum_id") or 0)
    module_id = (payload.get("module_id") or "").strip()
    learner_level = (payload.get("learner_level") or "").strip() or None
    task_id = int(task.get("Id") or 0)

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}
    if not org_id:
        return {"status": "failed", "error": "input_payload.org_id is required"}
    if not curriculum_id:
        return {"status": "failed", "error": "input_payload.curriculum_id is required"}
    if not module_id:
        return {"status": "failed", "error": "input_payload.module_id is required"}

    try:
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import (
            get_curriculum, get_learner_concepts,
            create_lesson_row, advance_curriculum_module, upsert_learner_concept,
        )
        from tools.teaching.llm import generate_lesson
        from tools.teaching.output import write_lesson_files
        from tools.research.research_planner import create_research_plan, run_research_planner_job
        from tools.research.agent import run_research_agent

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

        objectives: list[str] = target.get("objectives") or []
        module_title: str = target.get("title") or module_id
        known_concepts = get_learner_concepts(db, org_id, topic)

        # Source-ground the lesson via the research pipeline.
        research_query = f"{topic}: {module_title}"
        plan_result = create_research_plan(topic=research_query, org_id=org_id, defer_run=True)
        if plan_result.get("status") not in ("deferred", "pending"):
            return {"status": "failed", "error": f"research plan creation failed: {plan_result.get('error', plan_result.get('status'))}"}
        plan_id = int(plan_result.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": "research plan creation returned no plan_id"}

        run_research_planner_job(plan_id, queue_agent=False)
        agent_result = run_research_agent(plan_id)
        sources: list[dict] = agent_result.get("sources") or []

        plan_row = db._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
        research_text = ((plan_row.get("list") or [{}])[0]).get("paper_content") or ""

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
            lesson_markdown, anki_cards, session_summary, sources, checks,
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
            "sources": sources,
            "lesson_path": lesson_path,
            "cards_path": cards_path,
        }

    except Exception as exc:
        _log.error("teaching_lesson uncaught  org=%d topic=%r module=%s err=%s", org_id, topic[:80], module_id, exc, exc_info=True)
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
