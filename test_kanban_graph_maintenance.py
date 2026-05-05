"""Smoke test: graph_maintenance Kanban handler wraps graph_maintenance_job correctly."""
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

# Stub optional heavy deps before any imports that need them.
for _mod in ("falkordb", "chromadb", "playwright", "playwright.async_api"):
    if _mod not in sys.modules:
        _fake = types.ModuleType(_mod)
        if _mod == "falkordb":
            _fake.FalkorDB = MagicMock
        sys.modules[_mod] = _fake

_fake_memory = types.ModuleType("infra.memory")
_fake_memory.remember = MagicMock(return_value=[])
_fake_memory.recall = MagicMock(return_value=[])
_fake_memory.get_collection = MagicMock()
_fake_memory.client = MagicMock()
sys.modules.setdefault("infra.memory", _fake_memory)

import tools.graph_maintenance.agent  # noqa: E402


class TestKanbanGraphMaintenanceHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_input_payload_to_job(self):
        from workers.task_handlers.graph_maintenance import handle

        task = {
            "Id": 1,
            "task_type": "graph_maintenance",
            "input_payload": {"org_id": 1},
        }
        expected = {"status": "ok", "org_id": 1, "co_occurrence_added": 5, "decay": {"decayed": 10, "pruned": 2}}

        with patch("tools.graph_maintenance.agent.graph_maintenance_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"org_id": 1})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.graph_maintenance import handle

        task = {"Id": 2, "task_type": "graph_maintenance"}
        expected = {"status": "disabled"}

        with patch("tools.graph_maintenance.agent.graph_maintenance_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.graph_maintenance import handle

        task = {"Id": 3, "task_type": "graph_maintenance", "input_payload": {"org_id": 1}}

        with patch("tools.graph_maintenance.agent.graph_maintenance_job",
                   side_effect=RuntimeError("graph error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
