"""Kanban handler for graph_extract tasks."""
from __future__ import annotations

import asyncio

from workers.kanban import TaskHandler


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    from tools.graph_extract import _handle_graph_extract
    return await asyncio.to_thread(_handle_graph_extract, payload)


_type_check: TaskHandler = handle
