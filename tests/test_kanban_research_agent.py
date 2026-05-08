"""Smoke test: research_agent Kanban handler wraps run_research_agent correctly."""
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

import tools.research.agent  # noqa: E402


class TestKanbanResearchAgentHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_plan_id_to_job(self):
        from workers.task_handlers.research_agent import handle

        task = {
            "Id": 1,
            "task_type": "research_agent",
            "input_payload": {"plan_id": 42, "org_id": 1},
        }
        expected = {"status": "completed", "plan_id": 42}

        with patch("tools.research.agent.run_research_agent",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(42)
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.research_agent import handle

        task = {"Id": 2, "task_type": "research_agent"}
        expected = {"status": "not_found", "plan_id": 0}

        with patch("tools.research.agent.run_research_agent",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(0)
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.research_agent import handle

        task = {"Id": 3, "task_type": "research_agent", "input_payload": {"plan_id": 7}}

        with patch("tools.research.agent.run_research_agent",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
