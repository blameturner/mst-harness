"""Smoke test: insight_produce Kanban handler wraps insight_produce_job correctly."""
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
sys.modules.setdefault("infra.memory", _fake_memory)

import tools.insight.agent  # noqa: E402


class TestKanbanInsightProduceHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_payload_to_job(self):
        from workers.task_handlers.insight_produce import handle

        task = {"Id": 1, "task_type": "insight_produce", "input_payload": {"org_id": 1, "trigger": "manual"}}
        expected = {"status": "ok", "insight_id": 42}

        with patch("tools.insight.agent.insight_produce_job", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"org_id": 1, "trigger": "manual"})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.insight_produce import handle

        task = {"Id": 2, "task_type": "insight_produce"}
        expected = {"status": "disabled"}

        with patch("tools.insight.agent.insight_produce_job", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.insight_produce import handle

        task = {"Id": 3, "task_type": "insight_produce", "input_payload": {"org_id": 1}}

        with patch("tools.insight.agent.insight_produce_job", side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
