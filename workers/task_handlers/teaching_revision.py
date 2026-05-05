"""Kanban handler for 'teaching_revision' tasks.

Reads the parent teaching_lesson output_payload, applies revision instructions
via the lesson LLM, and produces updated lesson files. No re-search.
Input: {parent_task_id, revision_instructions}.
Output: same shape as teaching_lesson.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
from workers.kanban import TaskHandler

_log = logging.getLogger("teaching.revision")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    parent_task_id = int(payload.get("parent_task_id") or 0)
    revision_instructions = (payload.get("revision_instructions") or "").strip()
    task_id = int(task.get("Id") or 0)

    if not parent_task_id:
        return {"status": "failed", "error": "input_payload.parent_task_id is required"}
    if not revision_instructions:
        return {"status": "failed", "error": "input_payload.revision_instructions is required"}

    try:
        from infra.nocodb_client import NocodbClient
        from tools.teaching.db import get_lesson_row, update_lesson_row
        from tools.teaching.llm import generate_revision
        from tools.teaching.output import write_lesson_files

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
            return {"status": "failed", "error": f"parent task {parent_task_id} has no output_payload — may not have completed"}

        lesson_id = int(parent_output.get("lesson_id") or 0)
        if not lesson_id:
            return {"status": "failed", "error": "parent output_payload missing lesson_id"}

        lesson_row = get_lesson_row(db, lesson_id)
        if not lesson_row:
            return {"status": "failed", "error": f"teaching_lessons row {lesson_id} not found"}

        lesson_markdown = lesson_row.get("lesson_markdown") or ""
        raw_sources = lesson_row.get("sources") or "[]"
        try:
            sources: list[dict] = _json.loads(raw_sources) if isinstance(raw_sources, str) else raw_sources
        except _json.JSONDecodeError:
            sources = []

        revised_markdown, session_summary, anki_cards, checks = generate_revision(
            lesson_markdown=lesson_markdown,
            sources=sources,
            revision_instructions=revision_instructions,
        )

        lesson_path, cards_path = write_lesson_files(task_id, revised_markdown, anki_cards)
        update_lesson_row(db, lesson_id, revised_markdown, anki_cards, session_summary, checks, sources)

        curriculum_id = int(parent_output.get("curriculum_id") or 0)
        module_id = str(parent_output.get("module_id") or "")

        _log.info("teaching_revision done  lesson_id=%d parent_task=%d", lesson_id, parent_task_id)
        return {
            "status": "completed",
            "lesson_id": lesson_id,
            "revised_from_task_id": parent_task_id,
            "curriculum_id": curriculum_id,
            "module_id": module_id,
            "lesson_markdown": revised_markdown,
            "session_summary": session_summary,
            "anki_cards": anki_cards,
            "checks": checks,
            "sources": sources,
            "lesson_path": lesson_path,
            "cards_path": cards_path,
        }

    except Exception as exc:
        _log.error("teaching_revision uncaught  parent_task_id=%d err=%s", parent_task_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400], "parent_task_id": parent_task_id}


_type_check: TaskHandler = handle
