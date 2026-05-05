"""Kanban handler for research_review tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    plan_id = int(payload.get("plan_id") or 0)
    instructions = str(payload.get("instructions") or "")
    from tools.research.agent import review_research_paper
    return await asyncio.to_thread(review_research_paper, plan_id, instructions)


_type_check: TaskHandler = handle
