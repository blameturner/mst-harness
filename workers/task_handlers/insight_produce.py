"""Kanban handler for insight_produce tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.insight.agent import insight_produce_job
    return await asyncio.to_thread(insight_produce_job, payload)


_type_check: TaskHandler = handle
