"""Kanban handler for 'teaching_check' tasks.

Generates comprehension checks against an existing lesson without re-running
the lesson. Used for spaced repetition and ad-hoc assessment.
Input: {parent_task_id, concept_focus?, difficulty?, count?}.
Output: {lesson_id, checks, checks_path}.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler, TaskNotReady

_log = logging.getLogger("teaching.check")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    parent_task_id = int(payload.get("parent_task_id") or 0)
    concept_focus: list[str] = payload.get("concept_focus") or []
    difficulty = (payload.get("difficulty") or "mixed").strip()
    count = int(payload.get("count") or 5)
    task_id = int(task.get("Id") or 0)

    if not parent_task_id:
        return {"status": "failed", "error": "input_payload.parent_task_id is required"}

    try:
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import get_lesson_row
        from tools.teaching.llm import generate_checks
        from tools.teaching.output import write_checks_file

        db = NocodbClient()

        task_row_result = db._get("task_list", params={"where": f"(Id,eq,{parent_task_id})", "limit": 1})
        task_row = ((task_row_result.get("list") or [None])[0])
        if not task_row:
            return {"status": "failed", "error": f"parent task {parent_task_id} not found"}

        raw_output = task_row.get("output_payload") or {}
        try:
            parent_output: dict = _json.loads(raw_output) if isinstance(raw_output, str) else raw_output
        except _json.JSONDecodeError as exc:
            return {"status": "failed", "error": f"parent output_payload is not valid JSON: {exc}"}

        if not parent_output:
            raise TaskNotReady(
                f"parent task {parent_task_id} has no output_payload yet — re-queuing", delay_seconds=60
            )

        lesson_id = int(parent_output.get("lesson_id") or 0)
        if not lesson_id:
            return {"status": "failed", "error": "parent output_payload missing lesson_id"}

        lesson_row = get_lesson_row(db, lesson_id)
        if not lesson_row:
            return {"status": "failed", "error": f"teaching_lessons row {lesson_id} not found"}

        lesson_markdown = lesson_row.get("lesson_markdown") or ""

        # "mixed" difficulty runs a balanced set across all levels
        effective_difficulty = difficulty if difficulty != "mixed" else "introductory, working, and deep"

        checks = generate_checks(
            lesson_markdown=lesson_markdown,
            concept_focus=concept_focus,
            difficulty=effective_difficulty,
            count=count,
        )

        checks_path = write_checks_file(task_id, checks)

        _log.info("teaching_check done  lesson_id=%d count=%d parent_task=%d", lesson_id, len(checks), parent_task_id)
        return {
            "status": "completed",
            "lesson_id": lesson_id,
            "checks": checks,
            "checks_path": checks_path,
        }

    except TaskNotReady:
        raise
    except Exception as exc:
        _log.error("teaching_check uncaught  parent_task_id=%d err=%s", parent_task_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400], "parent_task_id": parent_task_id}


_type_check: TaskHandler = handle
