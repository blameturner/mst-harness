"""Pre-execution guardrails for project_* kanban tasks.

Call check_autonomy(db, task) at the top of every project_* handler.
Raises AutonomyBlock or AutonomyBackoff when a limit is exceeded.

kanban._execute catches these before the retry path:
  AutonomyBlock   → status='blocked', no retry
  AutonomyBackoff → re-queued with not_before delay, retry_count unchanged

Project tasks must be submitted with created_by=f"project:{project_id}"
and must include {"tokens_used": <int>} in their output_payload.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from infra.nocodb_client import NocodbClient

_log = logging.getLogger("project_autonomy")

TASK_TABLE = "task_list"

_DEFAULTS: dict[str, object] = {
    "autonomy_mode": "proposal-only",
    "max_tasks_per_hour": 4,
    "max_queued_proposals": 10,
    "max_daily_tokens": 100_000,
    "consecutive_failure_backoff": True,
}


class AutonomyBlock(Exception):
    """Terminal block — kanban sets status='blocked'. No retry."""


class AutonomyBackoff(Exception):
    """Temporary backoff — kanban re-queues with not_before delay."""

    def __init__(self, reason: str, delay_seconds: int) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


def check_autonomy(db: NocodbClient, task: dict) -> None:
    """Raises AutonomyBlock or AutonomyBackoff if any guardrail trips."""
    project_id = int((task.get("input_payload") or {}).get("project_id") or 0)
    _check_queue_depth(db, project_id)
    _check_hourly_rate(db, project_id)
    _check_daily_tokens(db, project_id)
    _check_consecutive_failures(db, project_id)


# ── settings ──────────────────────────────────────────────────────────────────

def _get_setting(project_id: int, key: str) -> object:
    try:
        from infra.settings import get_agent_setting
        val = get_agent_setting(f"project:{project_id}", key)
        if val is not None:
            return val
    except Exception:
        pass
    from infra.config import get_feature
    return get_feature("project", key, _DEFAULTS.get(key))


def _set_halted(project_id: int, halted: bool) -> None:
    try:
        from infra.settings import set_agent_setting
        set_agent_setting(f"project:{project_id}", "_halted", halted)
    except Exception:
        _log.error("failed to write _halted flag  project_id=%d", project_id, exc_info=True)


# ── guards ────────────────────────────────────────────────────────────────────

def _check_queue_depth(db: NocodbClient, project_id: int) -> None:
    limit = int(_get_setting(project_id, "max_queued_proposals") or _DEFAULTS["max_queued_proposals"])
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            "~and(task_type,eq,project_propose)"
            "~and((status,eq,ready)~or(status,eq,pending)~or(status,eq,claimed))"
        ),
        "limit": limit + 1,
    }).get("list", [])
    if len(rows) >= limit:
        raise AutonomyBlock(
            f"proposal queue at limit ({limit}); no new proposals until the queue drains"
        )


def _check_hourly_rate(db: NocodbClient, project_id: int) -> None:
    limit = int(_get_setting(project_id, "max_tasks_per_hour") or _DEFAULTS["max_tasks_per_hour"])
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            f"~and(started_at,gt,{one_hour_ago})"
        ),
        "limit": limit + 1,
    }).get("list", [])
    if len(rows) >= limit:
        raise AutonomyBlock(f"hourly rate limit reached ({limit}/hr); try again later")


def _check_daily_tokens(db: NocodbClient, project_id: int) -> None:
    cap = int(_get_setting(project_id, "max_daily_tokens") or _DEFAULTS["max_daily_tokens"])
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            f"~and(completed_at,gt,{today_start})"
            "~and(status,eq,done)"
        ),
        "limit": 500,
    }).get("list", [])
    total = 0
    for row in rows:
        raw = row.get("output_payload")
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        total += int(payload.get("tokens_used") or 0)
    if total >= cap:
        raise AutonomyBlock(
            f"daily token cap reached ({total:,}/{cap:,}); halted until midnight UTC"
        )


def _check_consecutive_failures(db: NocodbClient, project_id: int) -> None:
    enabled = _get_setting(project_id, "consecutive_failure_backoff")
    if not enabled:
        return

    if _get_setting(project_id, "_halted"):
        raise AutonomyBlock(
            "halted: manual resume required via POST /projects/{id}/autonomy/resume"
        )

    rows = db._get(TASK_TABLE, params={
        "where": (
            f"(created_by,eq,project:{project_id})"
            "~and((status,eq,done)~or(status,eq,failed)~or(status,eq,blocked))"
        ),
        "sort": "-completed_at",
        "limit": 6,
    }).get("list", [])

    streak = 0
    for row in rows:
        if row.get("status") == "failed":
            streak += 1
        else:
            break

    if streak >= 6:
        _set_halted(project_id, True)
        raise AutonomyBlock(
            f"halted: {streak} consecutive failures — manual resume required "
            "via POST /projects/{{id}}/autonomy/resume"
        )
    if streak >= 4:
        raise AutonomyBackoff(
            f"backoff: {streak} consecutive failures — pausing 30 minutes", 1800
        )
    if streak >= 2:
        raise AutonomyBackoff(
            f"backoff: {streak} consecutive failures — pausing 5 minutes", 300
        )