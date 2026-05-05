"""Kanban handler for discover_agent_run tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.enrichment.discover_agent import discover_agent_job
    return await asyncio.to_thread(discover_agent_job, payload)


_type_check: TaskHandler = handle
