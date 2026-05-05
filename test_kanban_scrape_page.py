"""Smoke test: scrape_page Kanban handler wraps scrape_page_job correctly."""
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

# Stub out heavy optional deps before any imports that need them.
for _mod in ("chromadb", "playwright", "playwright.async_api", "playwright.sync_api"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Stub infra.memory so tools.enrichment.scraper can be imported.
_fake_memory = types.ModuleType("infra.memory")
_fake_memory.remember = MagicMock(return_value=[])
sys.modules.setdefault("infra.memory", _fake_memory)

import tools.enrichment.scraper  # noqa: E402 — must follow the stubs above


class TestKanbanScrapePageHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_input_payload_to_job(self):
        from workers.task_handlers.scrape_page import handle

        task = {
            "Id": 1,
            "task_type": "scrape_page",
            "input_payload": {"target_id": 42, "org_id": 1},
        }
        expected = {"status": "ok", "target_id": 42, "url": "https://example.com",
                    "chunks": 3, "unchanged": False}

        with patch("tools.enrichment.scraper.scrape_page_job", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"target_id": 42, "org_id": 1})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload_calls_job_with_empty_dict(self):
        from workers.task_handlers.scrape_page import handle

        task = {"Id": 2, "task_type": "scrape_page"}
        expected = {"status": "idle"}

        with patch("tools.enrichment.scraper.scrape_page_job", return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.scrape_page import handle

        task = {"Id": 3, "task_type": "scrape_page", "input_payload": {"target_id": 99, "org_id": 1}}

        with patch("tools.enrichment.scraper.scrape_page_job", side_effect=RuntimeError("fetch failed")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
