"""Kanban handler for research_op tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.research.operations import run_research_op
    return await asyncio.to_thread(run_research_op, payload)


_type_check: TaskHandler = handle
