"""Kanban handler for extract_relationships tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.enrichment.relationships_extractor import extract_relationships_job
    return await asyncio.to_thread(extract_relationships_job, payload)


_type_check: TaskHandler = handle
