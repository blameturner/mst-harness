"""Dual-loop Kanban worker for the NocoDB task_list queue.

Loop A (run_llm_loop)     — one LLM-bound task at a time; respects the
                            chat-active gate and min-spacing between completions.
Loop B (run_non_llm_loop) — up to N non-LLM tasks concurrently; no LLM gating.

Start both as asyncio tasks under a single supervisor — see module footer.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

from infra.nocodb_client import NocodbClient

_log = logging.getLogger("kanban")

TASK_TABLE = "task_list"
MAX_RETRIES = 3
POLL_INTERVAL = 5.0

TaskHandler = Callable[[dict], Awaitable[dict]]


class TaskNotReady(Exception):
    """Raised by a handler when its prerequisite hasn't completed yet.

    kanban._execute re-queues the task with a not_before delay without
    consuming a retry slot — the same mechanism as AutonomyBackoff.
    """

    def __init__(self, reason: str, delay_seconds: int = 60) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class HandlerEntry:
    handler: TaskHandler
    llm_bound: bool


_registry: dict[str, HandlerEntry] = {}
_last_llm_done: float = 0.0  # time.monotonic(); 0.0 → no spacing wait before first task


def register(task_type: str, handler: TaskHandler, *, llm_bound: bool) -> None:
    _registry[task_type] = HandlerEntry(handler=handler, llm_bound=llm_bound)


# ── Loop A — LLM scheduler ───────────────────────────────────────────────────

async def run_llm_loop(db: NocodbClient) -> None:
    """Single coroutine. Claims and awaits one LLM-bound task at a time."""
    global _last_llm_done
    _log.info("kanban llm-loop started")
    from infra.config import get_feature
    grace_s = float(get_feature("kanban", "llm_grace_seconds", 2) or 2)
    spacing_s = float(get_feature("kanban", "llm_min_spacing_seconds", 2) or 2)

    while True:
        try:
            wait = spacing_s - (time.monotonic() - _last_llm_done)
            if wait > 0:
                await asyncio.sleep(wait)

            llm_types = {t for t, e in _registry.items() if e.llm_bound}
            if not llm_types:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            task = await asyncio.to_thread(_claim_next, db, llm_types)
            if task is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            payload = task.get("input_payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if payload.get("force_bypass_idle"):
                _log.info(
                    "kanban bypass_idle  row_id=%s  agent=%s  reason=bypass_idle flag set",
                    task.get("Id"), task.get("agent"),
                )
            else:
                while _chat_active():
                    _log.debug("kanban llm-loop gated on chat; sleeping %.1fs", grace_s)
                    await asyncio.sleep(grace_s)

            await _execute(db, task)
            _last_llm_done = time.monotonic()

        except asyncio.CancelledError:
            _log.info("kanban llm-loop cancelled")
            raise
        except Exception:
            _log.exception("kanban llm-loop unexpected error")
            await asyncio.sleep(POLL_INTERVAL)


# ── Loop B — non-LLM dispatcher ──────────────────────────────────────────────

async def run_non_llm_loop(db: NocodbClient) -> None:
    """Dispatches non-LLM tasks concurrently up to N."""
    _log.info("kanban non-llm-loop started")
    from infra.config import get_feature
    n = max(1, int(get_feature("kanban", "non_llm_concurrency", 4) or 4))
    sem = asyncio.Semaphore(n)
    active: set[asyncio.Task] = set()  # type: ignore[type-arg]  # reason: Task generic not needed at runtime

    while True:
        try:
            non_llm_types = {t for t, e in _registry.items() if not e.llm_bound}
            if not non_llm_types:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            await sem.acquire()
            task = await asyncio.to_thread(_claim_next, db, non_llm_types)
            if task is None:
                sem.release()
                await asyncio.sleep(POLL_INTERVAL)
                continue

            async def _run(t: dict) -> None:
                try:
                    await _execute(db, t)
                finally:
                    sem.release()

            t = asyncio.create_task(_run(task))
            active.add(t)
            t.add_done_callback(active.discard)

        except asyncio.CancelledError:
            _log.info("kanban non-llm-loop cancelled; draining %d tasks", len(active))
            for t in list(active):
                t.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            raise
        except Exception:
            _log.exception("kanban non-llm-loop unexpected error")
            await asyncio.sleep(POLL_INTERVAL)


# ── shared internals ─────────────────────────────────────────────────────────

def _claim_next(db: NocodbClient, task_types: set[str]) -> dict | None:
    if not db._has_table(TASK_TABLE):
        return None
    now = _iso_now()
    rows = db._get(TASK_TABLE, params={"where": "(status,eq,ready)", "sort": "CreatedAt", "limit": 50}).get("list", [])
    matching = [
        r for r in rows
        if r.get("task_type") in task_types
        and (not r.get("not_before") or r["not_before"] <= now)
    ]
    if not matching:
        return None

    row = matching[0]
    row_id = row["Id"]
    worker_id = uuid.uuid4().hex
    db._patch(TASK_TABLE, row_id, {"status": "claimed", "started_at": now, "claimed_by": worker_id})
    verify = db._get(TASK_TABLE, params={"where": f"(Id,eq,{row_id})", "limit": 1}).get("list", [])
    if not verify:
        return None
    claimed = verify[0]
    remote_claimed_by = claimed.get("claimed_by")
    if remote_claimed_by is not None:
        # Column exists: use it as the authoritative race-winner check
        if remote_claimed_by != worker_id:
            _log.debug("kanban claim race lost  row_id=%s  by=%s", row_id, remote_claimed_by)
            return None
    elif claimed.get("status") != "claimed":
        # Column absent (graceful degradation): fall back to status check
        _log.debug("kanban claim race lost  row_id=%s", row_id)
        return None
    _log.info("kanban claimed  row_id=%s  task_type=%s", row_id, claimed.get("task_type"))
    return claimed


async def _execute(db: NocodbClient, task: dict) -> None:
    row_id = task["Id"]
    task_type = task.get("task_type", "")
    retry_count = int(task.get("retry_count") or 0)
    entry = _registry.get(task_type)

    if entry is None:
        _log.error("kanban no handler  task_type=%s  row_id=%s", task_type, row_id)
        await asyncio.to_thread(_mark_failed, db, row_id, f"no handler for '{task_type}'")
        return

    try:
        from workers.project_autonomy import AutonomyBlock, AutonomyBackoff
    except ImportError:
        AutonomyBlock = None  # type: ignore[assignment,misc]  # reason: optional module
        AutonomyBackoff = None  # type: ignore[assignment,misc]  # reason: optional module

    try:
        output = await entry.handler(task)
        await asyncio.to_thread(_mark_done, db, row_id, output)
        _log.info("kanban done  row_id=%s  task_type=%s", row_id, task_type)
    except Exception as exc:
        if isinstance(exc, TaskNotReady):
            not_before = _iso_future(exc.delay_seconds)
            await asyncio.to_thread(_requeue_with_delay, db, row_id, not_before, str(exc))
            _log.info("kanban task-not-ready  row_id=%s  delay=%ds  reason=%s",
                      row_id, exc.delay_seconds, exc)
            return
        if AutonomyBackoff is not None and isinstance(exc, AutonomyBackoff):
            not_before = _iso_future(exc.delay_seconds)
            await asyncio.to_thread(_requeue_with_delay, db, row_id, not_before, str(exc))
            _log.info("kanban autonomy-backoff  row_id=%s  delay=%ds", row_id, exc.delay_seconds)
            return
        if AutonomyBlock is not None and isinstance(exc, AutonomyBlock):
            await asyncio.to_thread(_mark_blocked, db, row_id, str(exc))
            _log.info("kanban autonomy-blocked  row_id=%s  reason=%s", row_id, exc)
            return
        _log.warning("kanban error  row_id=%s  task_type=%s  retry=%d  err=%s",
                     row_id, task_type, retry_count, exc)
        if retry_count >= MAX_RETRIES - 1:
            await asyncio.to_thread(_mark_failed, db, row_id, str(exc))
        else:
            delay = 2 ** retry_count
            not_before = _iso_future(delay)
            await asyncio.to_thread(_mark_retry, db, row_id, retry_count + 1, not_before)


def _mark_done(db: NocodbClient, row_id: int, output: dict) -> None:
    db._patch(TASK_TABLE, row_id, {
        "status": "done", "output_payload": output, "completed_at": _iso_now(),
    })


def _mark_retry(db: NocodbClient, row_id: int, count: int, not_before: str) -> None:
    db._patch(TASK_TABLE, row_id, {
        "status": "ready", "retry_count": count, "not_before": not_before,
    })
    _log.info("kanban retry  row_id=%s  count=%d  not_before=%s", row_id, count, not_before)


def _mark_failed(db: NocodbClient, row_id: int, error: str) -> None:
    db._patch(TASK_TABLE, row_id, {
        "status": "failed", "error": error, "completed_at": _iso_now(),
    })
    _log.error("kanban failed  row_id=%s", row_id)


def _mark_blocked(db: NocodbClient, row_id: int, reason: str) -> None:
    db._patch(TASK_TABLE, row_id, {
        "status": "blocked", "error": reason, "completed_at": _iso_now(),
    })
    _log.warning("kanban blocked  row_id=%s  reason=%s", row_id, reason)


def _requeue_with_delay(db: NocodbClient, row_id: int, not_before: str, reason: str) -> None:
    # Reset retry_count so a future genuine error gets a full retry budget.
    db._patch(TASK_TABLE, row_id, {"status": "ready", "not_before": not_before, "retry_count": 0})
    _log.info("kanban requeue  row_id=%s  not_before=%s  reason=%s", row_id, not_before, reason)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _iso_future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(tzinfo=None).isoformat(timespec="seconds")


def _chat_active() -> bool:
    try:
        from workers.tool_queue import is_chat_active
        return is_chat_active()
    except Exception:
        return False  # fail-open: don't block LLM tasks if signal is unavailable


def submit(
    db: NocodbClient,
    task_type: str,
    payload: dict,
    *,
    status: str = "ready",
    created_by: str = "",
    agent: str = "",
    model: str = "",
    parent_task_id: int | None = None,
) -> int:
    """Insert a task into task_list. Returns the NocoDB row Id."""
    row: dict = {
        "task_type": task_type,
        "status": status,
        "input_payload": payload,
    }
    if created_by:
        row["created_by"] = created_by
    if agent:
        row["agent"] = agent
    if model:
        row["model"] = model
    if parent_task_id:
        row["parent_task_id"] = parent_task_id
    result = db._post(TASK_TABLE, row)
    _log.info("kanban submit  task_type=%s  status=%s  row_id=%s", task_type, status, result.get("Id"))
    return int(result.get("Id") or 0)


def count_inflight(db: NocodbClient, task_type: str) -> int:
    """Count claimed rows of a task_type in task_list."""
    try:
        data = db._get(TASK_TABLE, params={
            "where": f"(task_type,eq,{task_type})~and(status,eq,claimed)",
            "limit": 50,
        })
        return len(data.get("list", []))
    except Exception:
        _log.warning("kanban count_inflight failed  task_type=%s", task_type, exc_info=True)
        return 0
