"""Kanban handler for pathfinder_extract tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.enrichment.pathfinder import pathfinder_extract_job
    return await asyncio.to_thread(pathfinder_extract_job, payload)


_type_check: TaskHandler = handle
