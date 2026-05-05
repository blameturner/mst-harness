"""Smoke test: pathfinder_extract Kanban handler wraps pathfinder_extract_job correctly."""
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

import tools.enrichment.pathfinder  # noqa: E402


class TestKanbanPathfinderExtractHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_payload_to_job(self):
        from workers.task_handlers.pathfinder_extract import handle

        task = {"Id": 1, "task_type": "pathfinder_extract",
                "input_payload": {"suggested_id": 5, "org_id": 1}}
        expected = {"status": "ok", "inserted": 3}

        with patch("tools.enrichment.pathfinder.pathfinder_extract_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"suggested_id": 5, "org_id": 1})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.pathfinder_extract import handle

        task = {"Id": 2, "task_type": "pathfinder_extract"}
        expected = {"status": "not_found"}

        with patch("tools.enrichment.pathfinder.pathfinder_extract_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.pathfinder_extract import handle

        task = {"Id": 3, "task_type": "pathfinder_extract", "input_payload": {"suggested_id": 9}}

        with patch("tools.enrichment.pathfinder.pathfinder_extract_job",
                   side_effect=RuntimeError("fetch error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
