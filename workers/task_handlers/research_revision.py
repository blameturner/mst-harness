"""Kanban handler for 'research_revision' tasks.

Reads the parent 'research' task's output_payload, applies revision
instructions via review_research_paper, and produces new output files.
Input: {parent_task_id, revision_instructions}.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging

from workers.kanban import TaskHandler

_log = logging.getLogger("research.revision")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    parent_task_id = int(payload.get("parent_task_id") or 0)
    revision_instructions = (payload.get("revision_instructions") or "").strip()

    if not parent_task_id:
        return {"status": "failed", "error": "input_payload.parent_task_id is required"}
    if not revision_instructions:
        return {"status": "failed", "error": "input_payload.revision_instructions is required"}

    try:
        from infra.nocodb_client import NocodbClient
        from tools.research.agent import review_research_paper
        from tools.research.output import build_output_payload

        client = NocodbClient()

        task_row_result = client._get("task_list", params={"where": f"(Id,eq,{parent_task_id})", "limit": 1})
        task_row = ((task_row_result.get("list") or [None])[0])
        if not task_row:
            return {"status": "failed", "error": f"parent task {parent_task_id} not found"}

        raw_output = task_row.get("output_payload") or {}
        try:
            parent_output: dict = _json.loads(raw_output) if isinstance(raw_output, str) else raw_output
        except _json.JSONDecodeError as e:
            return {"status": "failed", "error": f"parent task output_payload is not valid JSON: {e}"}
        if not parent_output:
            return {"status": "failed", "error": f"parent task {parent_task_id} has no output_payload — may not have completed"}

        plan_id = int(parent_output.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": f"parent task {parent_task_id} output_payload missing plan_id"}

        review_result = review_research_paper(plan_id, revision_instructions)
        if review_result.get("status") != "completed":
            return {
                "status": "failed",
                "plan_id": plan_id,
                "error": review_result.get("error", review_result.get("status")),
            }

        row = client._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
        paper = ((row.get("list") or [{}])[0]).get("paper_content") or ""

        sources: list[dict] = parent_output.get("sources") or []
        doc_type: str = parent_output.get("doc_type") or "research_report"

        return build_output_payload(plan_id, doc_type, paper, sources)

    except Exception as exc:
        _log.error("research_revision uncaught  parent_task_id=%d  err=%s", parent_task_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400], "parent_task_id": parent_task_id}


_type_check: TaskHandler = handle
