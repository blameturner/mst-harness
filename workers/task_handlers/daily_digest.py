"""Kanban handler for daily_digest tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.digest.agent import daily_digest_job
    return await asyncio.to_thread(daily_digest_job, payload)


_type_check: TaskHandler = handle
