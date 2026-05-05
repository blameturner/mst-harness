"""Integration test: _fetch_corpus passes planner queries verbatim with the correct intent."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub heavy imports that agent.py pulls at module scope or lazily inside _fetch_corpus
_STUBS: dict[str, dict] = {
    "falkordb": {"FalkorDB": MagicMock},
    "chromadb": {},
    "playwright": {},
    "playwright.async_api": {},
    "infra.memory": {
        "remember": MagicMock(return_value=[]),
        "recall": MagicMock(return_value=[]),
        "get_collection": MagicMock(),
        "client": MagicMock(),
    },
    "workers.tool_queue": {
        "report_progress": MagicMock(),
        "is_job_cancelled": MagicMock(return_value=False),
        "JobCancelled": type("JobCancelled", (Exception,), {}),
        "current_job_id": MagicMock(return_value=None),
        "bind_job_id": MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))),
    },
}
for _mod, _attrs in _STUBS.items():
    if _mod not in sys.modules:
        _fake = types.ModuleType(_mod)
        for _k, _v in _attrs.items():
            setattr(_fake, _k, _v)
        sys.modules[_mod] = _fake

from tools.research.agent import _fetch_corpus, _research_intent_dict  # noqa: E402
from tools.search.intent import TASK_INTENT_SEARCH_EXPLICIT  # noqa: E402


class TestResearchSearchInvocation(unittest.TestCase):

    def test_intent_dict_uses_task_search_explicit(self):
        result = _research_intent_dict("LLM cost benchmarks")
        self.assertEqual(result["intent"], TASK_INTENT_SEARCH_EXPLICIT)

    def test_intent_dict_topic_excluded_from_entities(self):
        topic = "some research topic"
        result = _research_intent_dict(topic)
        self.assertNotIn(topic, result.get("entities", []))

    def test_fetch_corpus_passes_planner_queries_verbatim(self):
        planner_queries = [
            "GPT-4 API pricing enterprise 2024 benchmark",
            "OpenAI ChatGPT vs Anthropic Claude latency comparison",
        ]

        def fake_search(q, *, org_id, intent_dict, extraction_function_name):
            return (f"result for: {q}", [{"url": f"http://ex.com/{abs(hash(q))}", "title": "T"}], "high")

        with patch("tools.research.agent.run_web_search", side_effect=fake_search) as mock_search:
            corpus, sources = _fetch_corpus("LLM API pricing", planner_queries, org_id=1)

        self.assertEqual(mock_search.call_count, len(planner_queries))

        submitted_queries = [c.args[0] for c in mock_search.call_args_list]
        self.assertCountEqual(submitted_queries, planner_queries)

        for call in mock_search.call_args_list:
            self.assertEqual(call.kwargs["intent_dict"]["intent"], TASK_INTENT_SEARCH_EXPLICIT)

        self.assertIn("GPT-4 API pricing enterprise 2024 benchmark", corpus)
        self.assertIn("OpenAI ChatGPT vs Anthropic Claude latency comparison", corpus)
        self.assertEqual(len(sources), len(planner_queries))

    def test_fetch_corpus_excludes_failed_confidence(self):
        """Results with conf='failed' must not appear in the corpus."""
        planner_queries = ["good query", "bad query"]

        def fake_search(q, *, org_id, intent_dict, extraction_function_name):
            if "bad" in q:
                return ("failure context string", [], "failed")
            return ("good result", [{"url": "http://ok.com", "title": "T"}], "high")

        with patch("tools.research.agent.run_web_search", side_effect=fake_search):
            corpus, sources = _fetch_corpus("topic", planner_queries, org_id=1)

        self.assertIn("good result", corpus)
        self.assertNotIn("failure context string", corpus)


if __name__ == "__main__":
    unittest.main()
