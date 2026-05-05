"""Smoke test: corpus_maintenance Kanban handler wraps corpus_maintenance_job correctly."""
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

# Stub out heavy optional deps before imports that need them.
for _mod in ("chromadb", "playwright", "playwright.async_api", "playwright.sync_api"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

_fake_memory = types.ModuleType("infra.memory")
_fake_memory.remember = MagicMock(return_value=[])
_fake_memory.recall = MagicMock(return_value=[])
_fake_memory.get_collection = MagicMock()
_fake_memory.client = MagicMock()
sys.modules.setdefault("infra.memory", _fake_memory)

import tools.corpus_maintenance.agent  # noqa: E402 — must follow the stubs above


class TestKanbanCorpusMaintenanceHandler(unittest.IsolatedAsyncioTestCase):

    async def test_handle_passes_input_payload_to_job(self):
        from workers.task_handlers.corpus_maintenance import handle

        task = {
            "Id": 1,
            "task_type": "corpus_maintenance",
            "input_payload": {"org_id": 1},
        }
        expected = {
            "status": "ok",
            "org_id": 1,
            "rows_scanned": 42,
            "stale_refresh": {"candidates": 3, "enqueued": 2},
            "near_dup": {"domains": 1, "compared": 10, "clusters_with_dups": 1, "marked": 1},
        }

        with patch("tools.corpus_maintenance.agent.corpus_maintenance_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({"org_id": 1})
        self.assertEqual(result, expected)

    async def test_handle_empty_payload(self):
        from workers.task_handlers.corpus_maintenance import handle

        task = {"Id": 2, "task_type": "corpus_maintenance"}
        expected = {"status": "disabled"}

        with patch("tools.corpus_maintenance.agent.corpus_maintenance_job",
                   return_value=expected) as mock_job:
            result = await handle(task)

        mock_job.assert_called_once_with({})
        self.assertEqual(result, expected)

    async def test_handle_propagates_exception(self):
        from workers.task_handlers.corpus_maintenance import handle

        task = {"Id": 3, "task_type": "corpus_maintenance", "input_payload": {"org_id": 1}}

        with patch("tools.corpus_maintenance.agent.corpus_maintenance_job",
                   side_effect=RuntimeError("db error")):
            with self.assertRaises(RuntimeError):
                await handle(task)


if __name__ == "__main__":
    unittest.main()
