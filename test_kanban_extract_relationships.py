"""Smoke test: extract_relationships Kanban handler wraps extract_relationships_job correctly."""
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

import tools.enrichment.relationships_extractor  # noqa: E402


class TestKanbanExtractRelationshipsHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_payload_to_job(self):
        from workers.task_handlers.extract_relationships import handle

        payload = {"chunk_ids": ["a", "b"], "org_id": 1, "scrape_target_id": 5, "url": "http://x.com"}
        task = {"Id": 1, "task_type": "extract_relationships", "input_payload": payload}
        expected = {"status": "ok", "relationships": 3}

        with patch("tools.enrichment.relationships_extractor.extract_relationships_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with(payload)
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.extract_relationships import handle

        task = {"Id": 2, "task_type": "extract_relationships"}
        expected = {"status": "skipped"}

        with patch("tools.enrichment.relationships_extractor.extract_relationships_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.extract_relationships import handle

        task = {"Id": 3, "task_type": "extract_relationships", "input_payload": {"chunk_ids": ["x"]}}

        with patch("tools.enrichment.relationships_extractor.extract_relationships_job",
                   side_effect=RuntimeError("llm error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
