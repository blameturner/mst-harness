"""Smoke test: research_review Kanban handler wraps review_research_paper correctly."""
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


class TestKanbanResearchReviewHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_plan_id_and_instructions(self):
        from workers.task_handlers.research_review import handle

        task = {
            "Id": 1,
            "task_type": "research_review",
            "input_payload": {"plan_id": 42, "org_id": 1, "instructions": "be thorough"},
        }
        expected = {"status": "completed", "plan_id": 42}

        with patch("tools.research.agent.review_research_paper",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(42, "be thorough")
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.research_review import handle

        task = {"Id": 2, "task_type": "research_review"}
        expected = {"status": "not_found", "plan_id": 0}

        with patch("tools.research.agent.review_research_paper",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(0, "")
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.research_review import handle

        task = {"Id": 3, "task_type": "research_review", "input_payload": {"plan_id": 7}}

        with patch("tools.research.agent.review_research_paper",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
