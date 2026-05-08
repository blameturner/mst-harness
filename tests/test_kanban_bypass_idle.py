import asyncio
import unittest
from unittest.mock import MagicMock, patch

import workers.kanban as kanban


class KanbanBypassIdleTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._saved_registry = dict(kanban._registry)
        self._saved_last = kanban._last_llm_done
        kanban._registry.clear()
        kanban._last_llm_done = 0.0

    def tearDown(self):
        kanban._registry.clear()
        kanban._registry.update(self._saved_registry)
        kanban._last_llm_done = self._saved_last

    async def test_bypass_idle_runs_when_chat_active(self):
        done = asyncio.Event()
        executed: list[int] = []

        async def dummy_handler(t: dict) -> dict:
            executed.append(int(t["Id"]))
            done.set()
            return {}

        kanban.register("bypass_task", dummy_handler, llm_bound=True)

        task = {
            "Id": 99,
            "task_type": "bypass_task",
            "agent": "admin",
            "retry_count": "0",
            "input_payload": {"force_bypass_idle": True},
        }
        call_count = [0]

        def fake_claim(_db, _types):
            call_count[0] += 1
            return task if call_count[0] == 1 else None

        with patch("workers.kanban._claim_next", side_effect=fake_claim), \
             patch("workers.kanban._chat_active", return_value=True), \
             patch("workers.kanban._mark_done"), \
             patch("infra.config.get_feature", return_value=0):
            loop_task = asyncio.create_task(kanban.run_llm_loop(MagicMock()))
            try:
                await asyncio.wait_for(done.wait(), timeout=2.0)
            finally:
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass

        self.assertEqual(executed, [99])


if __name__ == "__main__":
    unittest.main()
