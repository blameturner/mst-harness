"""Kanban handler for pa_topic_research tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.pa.background import pa_topic_research_job
    return await asyncio.to_thread(pa_topic_research_job, payload)


_type_check: TaskHandler = handle
