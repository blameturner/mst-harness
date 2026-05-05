"""Kanban handler for summarise_page tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.enrichment.summariser import summarise_page_job
    return await asyncio.to_thread(summarise_page_job, payload)


_type_check: TaskHandler = handle
