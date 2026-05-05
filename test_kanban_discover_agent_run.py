"""Smoke test: discover_agent_run Kanban handler wraps discover_agent_job correctly."""
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

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

import tools.enrichment.discover_agent  # noqa: E402


class TestKanbanDiscoverAgentRunHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_payload_to_job(self):
        from workers.task_handlers.discover_agent_run import handle

        task = {"Id": 1, "task_type": "discover_agent_run", "input_payload": {"org_id": 1}}
        expected = {"status": "ok", "discovered": 5}

        with patch("tools.enrichment.discover_agent.discover_agent_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"org_id": 1})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.discover_agent_run import handle

        task = {"Id": 2, "task_type": "discover_agent_run"}
        expected = {"status": "disabled"}

        with patch("tools.enrichment.discover_agent.discover_agent_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.discover_agent_run import handle

        task = {"Id": 3, "task_type": "discover_agent_run", "input_payload": {"org_id": 1}}

        with patch("tools.enrichment.discover_agent.discover_agent_job",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
