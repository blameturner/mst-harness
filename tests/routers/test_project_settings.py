"""Tests for extended autonomy settings fields."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_autonomy_settings_has_new_fields():
    from app.routers.tasks import AutonomySettings
    fields = AutonomySettings.model_fields
    assert "model_agent" in fields
    assert "model_po" in fields
    assert "staging_branch" in fields
    assert "architectural_rules" in fields


def test_trigger_routes_registered():
    from app.routers.tasks import router
    paths = [r.path for r in router.routes]
    assert any("trigger/feature" in p for p in paths), f"missing trigger/feature in {paths}"
    assert any("trigger/propose" in p for p in paths), f"missing trigger/propose in {paths}"


def test_trigger_feature_request_model():
    from app.routers.tasks import TriggerFeatureRequest
    req = TriggerFeatureRequest(feature_description="Add auth", branch_name="feature/auth")
    assert req.feature_description == "Add auth"
    assert req.architect_context is None


def test_trigger_propose_request_model():
    from app.routers.tasks import TriggerProposeRequest
    req = TriggerProposeRequest()
    assert req.model is None


def test_project_index_handler_importable():
    import workers.task_handlers.project_index as m
    assert callable(m.handle)
