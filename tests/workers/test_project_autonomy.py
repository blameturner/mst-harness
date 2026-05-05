"""Tests for workers.project_autonomy guardrails."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from workers.project_autonomy import AutonomyBackoff, AutonomyBlock, _DEFAULTS, check_autonomy

PROJECT_ID = 42


def _task(project_id: int = PROJECT_ID) -> dict:
    return {"Id": 1, "task_type": "project_propose", "input_payload": {"project_id": project_id}}


def _db(*call_results: list) -> MagicMock:
    db = MagicMock()
    db._get.side_effect = [{"list": rows} for rows in call_results]
    return db


def _no_setting(project_id: int, key: str) -> object:
    """Simulate no DB override — return the config.json default."""
    return _DEFAULTS.get(key)


# ── queue depth ───────────────────────────────────────────────────────────────

def test_queue_depth_under_limit_proceeds():
    db = _db([], [], [], [])  # queue, hourly, daily, failures
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_queue_depth_at_limit_blocks():
    rows = [{"Id": i} for i in range(10)]
    db = _db(rows)  # blocks on first check; remaining calls not reached
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="proposal queue"):
            check_autonomy(db, _task())


# ── hourly rate ───────────────────────────────────────────────────────────────

def test_hourly_rate_under_limit_proceeds():
    db = _db([], [], [], [])  # queue, hourly, daily, failures
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_hourly_rate_at_limit_blocks():
    db = _db([], [{"Id": i} for i in range(4)])
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="hourly rate"):
            check_autonomy(db, _task())


# ── daily tokens ──────────────────────────────────────────────────────────────

def test_daily_tokens_under_cap_proceeds():
    rows = [{"output_payload": json.dumps({"tokens_used": 10_000})} for _ in range(3)]
    db = _db([], [], rows, [])  # queue, hourly, daily, failures
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_daily_tokens_over_cap_blocks():
    rows = [{"output_payload": json.dumps({"tokens_used": 50_000})} for _ in range(3)]
    db = _db([], [], rows)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBlock, match="daily token"):
            check_autonomy(db, _task())


def test_daily_tokens_missing_field_counts_zero():
    rows = [{"output_payload": json.dumps({})} for _ in range(3)]
    db = _db([], [], rows, [])  # queue, hourly, daily, failures
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


# ── consecutive failures ──────────────────────────────────────────────────────

def test_no_failures_proceeds():
    db = _db([], [], [], [])
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_two_consecutive_failures_backoff_5min():
    db = _db([], [], [], [{"status": "failed"}, {"status": "failed"}])
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBackoff) as exc_info:
            check_autonomy(db, _task())
    assert exc_info.value.delay_seconds == 300


def test_four_consecutive_failures_backoff_30min():
    db = _db([], [], [], [{"status": "failed"}] * 4)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with pytest.raises(AutonomyBackoff) as exc_info:
            check_autonomy(db, _task())
    assert exc_info.value.delay_seconds == 1800


def test_six_consecutive_failures_halts():
    db = _db([], [], [], [{"status": "failed"}] * 6)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        with patch("workers.project_autonomy._set_halted") as mock_halt:
            with pytest.raises(AutonomyBlock, match="halted"):
                check_autonomy(db, _task())
        mock_halt.assert_called_once_with(PROJECT_ID, True)


def test_mixed_failures_not_consecutive_proceeds():
    rows = [{"status": "failed"}, {"status": "done"}, {"status": "failed"}]
    db = _db([], [], [], rows)
    with patch("workers.project_autonomy._get_setting", side_effect=_no_setting):
        check_autonomy(db, _task())


def test_halted_flag_blocks_regardless_of_streak():
    db = _db([], [], [], [])
    def _halted_setting(project_id: int, key: str) -> object:
        if key == "_halted":
            return True
        return _DEFAULTS.get(key)
    with patch("workers.project_autonomy._get_setting", side_effect=_halted_setting):
        with pytest.raises(AutonomyBlock, match="halted"):
            check_autonomy(db, _task())


def test_backoff_disabled_skips_failure_check():
    db = _db([], [], [])
    def _no_backoff(project_id: int, key: str) -> object:
        return False if key == "consecutive_failure_backoff" else None
    with patch("workers.project_autonomy._get_setting", side_effect=_no_backoff):
        check_autonomy(db, _task())
    assert db._get.call_count == 3