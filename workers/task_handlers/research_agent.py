"""Kanban handler for research_agent tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    plan_id = int(payload.get("plan_id") or 0)
    from tools.research.agent import run_research_agent
    return await asyncio.to_thread(run_research_agent, plan_id)


_type_check: TaskHandler = handle
