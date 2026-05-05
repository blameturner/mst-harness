"""Kanban handler for 'research' tasks.

Orchestrates plan creation, query generation, and paper synthesis in a single
task. Input: {topic, org_id, doc_type?}. Output: structured output_payload
with report_markdown, findings, sources, and paths to written files.
"""
from __future__ import annotations

import asyncio
import logging

from workers.kanban import TaskHandler

_log = logging.getLogger("research.handler")


async def handle(task: dict) -> dict:
    payload = task.get("input_payload") or {}
    return await asyncio.to_thread(_run, payload)


def _run(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    org_id = int(payload.get("org_id") or 0)
    doc_type_hint = (payload.get("doc_type") or "").strip() or None

    if not topic:
        return {"status": "failed", "error": "input_payload.topic is required"}

    try:
        import json as _json
        from infra.nocodb_client import NocodbClient
        from tools.research.research_planner import create_research_plan, run_research_planner_job
        from tools.research.agent import run_research_agent
        from tools.research.output import build_output_payload

        plan_result = create_research_plan(topic=topic, org_id=org_id, defer_run=True)
        if plan_result.get("status") not in ("deferred", "pending"):
            return {"status": "failed", "error": f"plan creation failed: {plan_result.get('error', plan_result.get('status'))}"}
        plan_id = int(plan_result.get("plan_id") or 0)
        if not plan_id:
            return {"status": "failed", "error": "plan creation returned no plan_id"}

        client = NocodbClient()

        if doc_type_hint:
            try:
                row = client._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
                existing = (row.get("list") or [{}])[0]
                schema = _json.loads(existing.get("schema") or "{}")
                schema["_doc_type"] = doc_type_hint
                client._patch("research_plans", plan_id, {"schema": _json.dumps(schema)})
            except Exception as e:
                _log.warning("doc_type stash failed  plan_id=%d  err=%s", plan_id, e)
                # Non-fatal: research continues with default doc_type

        planner_result = run_research_planner_job(plan_id, queue_agent=False)
        if planner_result.get("status") in ("failed", "not_found", "disabled"):
            return {"status": "failed", "plan_id": plan_id, "error": planner_result.get("error", planner_result["status"])}

        agent_result = run_research_agent(plan_id)
        if agent_result.get("status") != "completed":
            return {"status": "failed", "plan_id": plan_id, "error": agent_result.get("error", agent_result.get("status"))}

        row = client._get("research_plans", params={"where": f"(Id,eq,{plan_id})", "limit": 1})
        paper = ((row.get("list") or [{}])[0]).get("paper_content") or ""

        sources: list[dict] = agent_result.get("sources") or []
        final_doc_type: str = agent_result.get("doc_type") or doc_type_hint or "research_report"

        return build_output_payload(plan_id, final_doc_type, paper, sources)

    except Exception as exc:
        _log.error("research handler uncaught  topic=%r  err=%s", topic[:80], exc, exc_info=True)
        return {"status": "failed", "error": str(exc)[:400], "topic": topic}


_type_check: TaskHandler = handle
