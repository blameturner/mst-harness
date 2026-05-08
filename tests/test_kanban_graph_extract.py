"""Smoke test: graph_extract Kanban handler wraps _handle_graph_extract correctly."""
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

import tools.graph_extract  # noqa: E402


class TestKanbanGraphExtractHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_payload_to_job(self):
        from workers.task_handlers.graph_extract import handle

        task = {
            "Id": 1,
            "task_type": "graph_extract",
            "input_payload": {"user_text": "hello", "assistant_text": "hi", "conversation_id": 5, "org_id": 1},
        }
        expected = {"status": "ok"}

        with patch("tools.graph_extract._handle_graph_extract", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(task["input_payload"])
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.graph_extract import handle

        task = {"Id": 2, "task_type": "graph_extract"}
        expected = {"written": 0, "error": "empty input"}

        with patch("tools.graph_extract._handle_graph_extract", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.graph_extract import handle

        task = {"Id": 3, "task_type": "graph_extract", "input_payload": {"user_text": "x"}}

        with patch("tools.graph_extract._handle_graph_extract", side_effect=RuntimeError("graph error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
