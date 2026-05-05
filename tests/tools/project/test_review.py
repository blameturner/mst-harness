from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.project.review import (
    build_review_context,
    parse_verdict,
    _parse_verdict_json,
)


def test_build_review_context_includes_all_sections():
    ctx = build_review_context("+ new line", "Add auth", "Python FastAPI app", "No global state")
    assert "## Feature Description" in ctx
    assert "## Repo Summary" in ctx
    assert "## Architectural Rules" in ctx
    assert "## Diff" in ctx
    assert "Add auth" in ctx


def test_build_review_context_omits_empty_summary():
    ctx = build_review_context("+ new line", "Add auth", "", "")
    assert "## Repo Summary" not in ctx
    assert "## Architectural Rules" not in ctx


def test_parse_verdict_approve():
    assert parse_verdict({"verdict": "approve"}) == "approve"


def test_parse_verdict_reject():
    assert parse_verdict({"verdict": "reject"}) == "reject"


def test_parse_verdict_revise():
    assert parse_verdict({"verdict": "revise"}) == "revise"


def test_parse_verdict_invalid_raises():
    with pytest.raises(ValueError, match="unexpected verdict"):
        parse_verdict({"verdict": "maybe"})


def test_parse_verdict_json_strips_markdown_fencing():
    raw = '```json\n{"verdict": "approve", "rationale": "looks good"}\n```'
    result = _parse_verdict_json(raw)
    assert result["verdict"] == "approve"


def test_parse_verdict_json_raises_on_non_json():
    with pytest.raises(ValueError, match="non-JSON"):
        _parse_verdict_json("sorry, I cannot review this")
