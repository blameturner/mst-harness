"""Kanban handler for 'research' tasks.

Two-phase execution:

  Phase 1 (no plan_id in payload)
    - Create a research plan, optionally stash doc_type hint
    - Submit research_planner (queue_agent=True — auto-queues research_agent)
    - Patch this task's input_payload with plan_id
    - Raise TaskNotReady

  Phase 2 (plan_id present)
    - Poll plan status; wait if still running
    - On completion: build and return structured output_payload

Input:  {topic, org_id, doc_type?}
Output: {plan_id, doc_type, report_markdown, findings, sources, paths}
"""
from __future__ import annotations

import asyncio
import json as _json
import logging

from workers.kanban import TaskHandler, TaskNotReady

_log = logging.getLogger("research.handler")

_RESEARCH_IN_PROGRESS = frozenset({"pending", "planned", "searching", "synthesizing", "queued"})


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, task, payload)


def _run(task: dict, payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}

    if payload.get("plan_id"):
        return _output_phase(payload)
    return _plan_phase(task, payload, topic, org_id)


def _plan_phase(task: dict, payload: dict, topic: str, org_id: int) -> dict:
    """Phase 1: create plan, submit planner+agent pipeline, wait."""
    try:
        from infra.nocodb_client import NocodbClient
        from tools.research.research_planner import create_research_plan
        from workers import kanban

        doc_type_hint = (payload.get("doc_type") or "").strip() or None
        task_id = int(task.get("Id") or 0)

        plan_result = create_research_plan(topic=topic, org_id=org_id, defer_run=True)
        if plan_result.get("status") not in ("deferred", "pending"):
            return {"status": "failed", "error": f"plan creation failed: {plan_result.get('error', plan_result.get('status'))}"}
        plan_id = int(plan_result.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": "plan creation returned no plan_id"}

        db = NocodbClient()

        if doc_type_hint:
            try:
                row = db._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
                existing = (row.get("list") or [{}])[0]
                schema = _json.loads(existing.get("schema") or "{}")
                schema["_doc_type"] = doc_type_hint
                db._patch("research_plans", plan_id, {"schema": _json.dumps(schema)})
            except Exception as e:
                _log.warning("doc_type stash failed  plan_id=%d  err=%s", plan_id, e)

        # queue_agent=True: planner auto-queues research_agent on completion
        kanban.submit(
            db, "research_planner", {"plan_id": plan_id, "org_id": org_id},
            created_by="research_handler",
        )

        db._patch("task_list", task_id, {"input_payload": {**payload, "plan_id": plan_id}})
        _log.info("research handler plan_phase  task_id=%d  plan_id=%d", task_id, plan_id)
        raise TaskNotReady(f"research plan {plan_id} submitted", delay_seconds=90)

    except TaskNotReady:
        raise
    except Exception as exc:
        _log.error("research handler plan_phase  topic=%r  err=%s", topic[:80], exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400], "topic": topic}


def _output_phase(payload: dict) -> dict:
    """Phase 2: research pipeline complete — return structured output."""
    plan_id = int(payload.get("plan_id") or 0)
    doc_type_hint = (payload.get("doc_type") or "").strip() or None

    try:
        from infra.nocodb_client import NocodbClient
        from tools.research.output import build_output_payload

        db = NocodbClient()
        row = db._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
        plan = (row.get("list") or [{}])[0]
        if not plan:
            return {"status": "failed", "error": f"research plan {plan_id} not found"}

        plan_status = (plan.get("status") or "").strip()
        if plan_status in ("failed", "error"):
            return {"status": "failed", "error": f"research plan {plan_id} failed: {plan.get('error_message', '')[:200]}"}
        if plan_status != "completed":
            raise TaskNotReady(f"research plan {plan_id} status={plan_status!r}", delay_seconds=120)

        paper = (plan.get("paper_content") or "").strip()
        if not paper:
            return {"status": "failed", "plan_id": plan_id, "error": "plan completed but paper_content is empty"}

        schema = _json.loads(plan.get("schema") or "{}")
        doc_type = schema.get("_doc_type") or doc_type_hint or "research_report"

        return build_output_payload(plan_id, doc_type, paper, [])

    except TaskNotReady:
        raise
    except Exception as exc:
        _log.error("research handler output_phase  plan_id=%d  err=%s", plan_id, exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400]}


_type_check: TaskHandler = handle
