"""Kanban handler for corpus_maintenance tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.corpus_maintenance.agent import corpus_maintenance_job
    return await asyncio.to_thread(corpus_maintenance_job, payload)


_type_check: TaskHandler = handle
