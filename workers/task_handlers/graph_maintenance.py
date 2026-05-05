"""Kanban handler for graph_maintenance tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.graph_maintenance.agent import graph_maintenance_job
    return await asyncio.to_thread(graph_maintenance_job, payload)


_type_check: TaskHandler = handle
