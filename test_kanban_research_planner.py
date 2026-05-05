"""Smoke test: research_planner Kanban handler wraps run_research_planner_job correctly."""
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

import tools.research.research_planner  # noqa: E402


class TestKanbanResearchPlannerHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_plan_id_to_job(self):
        from workers.task_handlers.research_planner import handle

        task = {
            "Id": 1,
            "task_type": "research_planner",
            "input_payload": {"plan_id": 42, "org_id": 1},
        }
        expected = {"status": "queued", "plan_id": 42, "queries": 5, "agent_task_id": 99}

        with patch("tools.research.research_planner.run_research_planner_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(42)
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.research_planner import handle

        task = {"Id": 2, "task_type": "research_planner"}
        expected = {"status": "not_found", "plan_id": 0}

        with patch("tools.research.research_planner.run_research_planner_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(0)
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.research_planner import handle

        task = {"Id": 3, "task_type": "research_planner", "input_payload": {"plan_id": 7}}

        with patch("tools.research.research_planner.run_research_planner_job",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
