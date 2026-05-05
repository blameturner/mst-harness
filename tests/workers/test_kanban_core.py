"""Unit tests for workers.kanban core infrastructure (claim, requeue, inflight)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, call, patch


def _make_db(patch_returns=None, get_returns=None):
    db = MagicMock()
    db._patch.return_value = patch_returns or {}
    db._get.return_value = {"list": get_returns or []}
    db._has_table.return_value = True
    return db


# ── _requeue_with_delay ───────────────────────────────────────────────────────

def test_requeue_with_delay_resets_retry_count():
    """Re-queue after AutonomyBackoff must reset retry_count so the task
    doesn't hit the permanent-failure threshold on its next real error."""
    from workers.kanban import _requeue_with_delay

    db = _make_db()
    _requeue_with_delay(db, row_id=99, not_before="2099-01-01T00:00:00+00:00", reason="backoff")

    db._patch.assert_called_once()
    patch_payload = db._patch.call_args.args[2]
    assert patch_payload.get("retry_count") == 0, (
        "retry_count must be reset to 0 so future errors don't immediately fail the task"
    )
    assert patch_payload["status"] == "ready"


# ── _claim_next ───────────────────────────────────────────────────────────────

def test_claim_next_returns_none_when_claimed_by_differs():
    """Simulates another worker winning the race: the verify re-read has a
    different claimed_by value, so _claim_next must return None."""
    from workers.kanban import _claim_next

    db = MagicMock()
    db._has_table.return_value = True
    # Initial query returns one ready row
    ready_row = {"Id": 42, "task_type": "scrape_page", "status": "ready"}
    # Verify re-read returns the row claimed by a different worker
    verify_row = {"Id": 42, "task_type": "scrape_page", "status": "claimed", "claimed_by": "other-worker-id"}

    db._get.side_effect = [
        {"list": [ready_row]},   # initial query
        {"list": [verify_row]},  # verify re-read
    ]

    result = _claim_next(db, {"scrape_page"})
    assert result is None, "Must return None when verify shows a different claimed_by"


def test_claim_next_returns_row_when_claimed_by_matches():
    """Verify that a successful claim (claimed_by matches) is returned."""
    from workers.kanban import _claim_next
    import uuid

    db = MagicMock()
    db._has_table.return_value = True

    ready_row = {"Id": 7, "task_type": "scrape_page", "status": "ready"}
    captured_worker_id: list[str] = []

    def fake_patch(table, row_id, payload):
        captured_worker_id.append(payload.get("claimed_by", ""))
        return {}

    def fake_get(table, params=None):
        if captured_worker_id:
            return {"list": [{"Id": 7, "task_type": "scrape_page", "status": "claimed",
                               "claimed_by": captured_worker_id[0]}]}
        return {"list": [ready_row]}

    db._patch.side_effect = fake_patch
    db._get.side_effect = fake_get

    result = _claim_next(db, {"scrape_page"})
    assert result is not None
    assert result["Id"] == 7


# ── count_inflight ────────────────────────────────────────────────────────────

def test_count_inflight_logs_and_returns_zero_on_db_exception(caplog):
    """DB failure should be logged, not silently swallowed."""
    from workers.kanban import count_inflight

    db = MagicMock()
    db._get.side_effect = RuntimeError("connection refused")

    with caplog.at_level(logging.WARNING, logger="kanban"):
        result = count_inflight(db, "scrape_page")

    assert result == 0
    assert any("connection refused" in r.message or "count_inflight" in r.message
               for r in caplog.records), "DB error must be logged"
