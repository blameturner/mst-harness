"""Smoke test: research_op Kanban handler wraps run_research_op correctly."""
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

import tools.research.operations  # noqa: E402


class TestKanbanResearchOpHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_full_payload(self):
        from workers.task_handlers.research_op import handle

        task = {
            "Id": 1,
            "task_type": "research_op",
            "input_payload": {"plan_id": 42, "kind": "fact_check", "params": {}},
        }
        expected = {"status": "completed", "plan_id": 42}

        with patch("tools.research.operations.run_research_op",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"plan_id": 42, "kind": "fact_check", "params": {}})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.research_op import handle

        task = {"Id": 2, "task_type": "research_op"}
        expected = {"status": "failed", "error": "no kind"}

        with patch("tools.research.operations.run_research_op",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.research_op import handle

        task = {"Id": 3, "task_type": "research_op", "input_payload": {"plan_id": 7, "kind": "x"}}

        with patch("tools.research.operations.run_research_op",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
