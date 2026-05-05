"""Kanban handler for graph_resolve_entities tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.graph_maintenance.agent import graph_resolve_entities_job
    return await asyncio.to_thread(graph_resolve_entities_job, payload)


_type_check: TaskHandler = handle
