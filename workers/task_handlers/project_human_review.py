"""Kanban handler for project_human_review tasks — zero LLM calls.

Routes based on whether the human provided feedback:
  - No feedback (approved as-is) → enqueue project_review (ready)
  - Feedback present             → enqueue project_revise (ready)

Input payload:
  project_id: int
  pr_id: int
  branch_name: str
  feature_description: str
  architect_context: str | None
  revision_count: int
  changed_paths: list[str]
  human_feedback: str | None    — set by the approve endpoint
"""
from __future__ import annotations

import asyncio
import logging

from workers.kanban import TaskHandler

_log = logging.getLogger(__name__)


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    from workers.project_autonomy import check_autonomy
    from infra.nocodb_client import NocodbClient
    from workers import kanban as _kanban

    project_id = int(payload.get("project_id") or 0)
    if not project_id:
        return {"status": "failed", "error": "input_payload.project_id required"}

    db = NocodbClient()
    check_autonomy(db, task)

    human_feedback: str | None = payload.get("human_feedback") or None
    base = {k: payload[k] for k in payload if k != "human_feedback"}

    if human_feedback:
        _kanban.submit(
            db, "project_revise",
            {**base, "human_feedback": human_feedback},
            created_by=f"project:{project_id}",
            agent=f"project:{project_id}",
        )
        _log.info("project_human_review → revise  project=%d  feedback_chars=%d", project_id, len(human_feedback))
        return {"status": "done", "action": "revise"}

    _kanban.submit(
        db, "project_review", base,
        created_by=f"project:{project_id}",
        agent=f"project:{project_id}",
    )
    _log.info("project_human_review → review  project=%d", project_id)
    return {"status": "done", "action": "review"}


_type_check: TaskHandler = handle
