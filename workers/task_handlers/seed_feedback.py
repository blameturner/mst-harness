"""Kanban handler for seed_feedback tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.seed_feedback.agent import seed_feedback_job
    return await asyncio.to_thread(seed_feedback_job, payload)


_type_check: TaskHandler = handle
