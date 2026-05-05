"""Kanban handler for research_planner tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    plan_id = int(payload.get("plan_id") or 0)
    from tools.research.research_planner import run_research_planner_job
    return await asyncio.to_thread(run_research_planner_job, plan_id)


_type_check: TaskHandler = handle
