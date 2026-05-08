from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from shared.model_client import (
    CompletionResult,
    LlamaCppBackend,
    ModelClient,
    OpenRouterBackend,
    _parse_openai_response,
    build_model_client,
)

_RESOLVER = lambda m: "http://localhost:8080"  # noqa: E731


def _openai_response(text: str, model: str = "test-model", tokens_in: int = 10, tokens_out: int = 20) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def _mock_httpx_response(payload: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _async_client_with_response(payload: dict, status_code: int = 200) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=_mock_httpx_response(payload, status_code))
    return client


def _sync_client_with_response(payload: dict, status_code: int = 200) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    client.post = MagicMock(return_value=_mock_httpx_response(payload, status_code))
    return client


class ParseOpenAIResponseTests(unittest.TestCase):
    def test_parses_complete_response(self):
        data = _openai_response("hello", model="llama3", tokens_in=5, tokens_out=15)
        result = _parse_openai_response(data, "llama3")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.model_used, "llama3")
        self.assertEqual(result.tokens_in, 5)
        self.assertEqual(result.tokens_out, 15)
        self.assertEqual(result.finish_reason, "stop")
        self.assertIsNone(result.error)

    def test_falls_back_to_requested_model_when_response_omits_model(self):
        data = _openai_response("hi")
        del data["model"]
        result = _parse_openai_response(data, "fallback-model")
        self.assertEqual(result.model_used, "fallback-model")

    def test_returns_error_result_on_malformed_response(self):
        result = _parse_openai_response({}, "m")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.text, "")
        self.assertEqual(result.finish_reason, "error")

    def test_falls_back_to_reasoning_content_when_content_empty(self):
        data = {
            "model": "deepseek/r1",
            "choices": [{"message": {"content": "", "reasoning_content": "my reasoning"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 5},
        }
        result = _parse_openai_response(data, "deepseek/r1")
        self.assertEqual(result.text, "my reasoning")
        self.assertIsNone(result.error)


class LlamaCppBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_completion(self):
        payload = _openai_response("local answer", model="llama3.1")
        backend = LlamaCppBackend(_RESOLVER, client=_async_client_with_response(payload))
        result = await backend.complete([{"role": "user", "content": "hi"}], "llama3.1")
        self.assertEqual(result.text, "local answer")
        self.assertEqual(result.model_used, "llama3.1")
        self.assertIsNone(result.error)

    async def test_passes_kwargs_in_payload(self):
        payload = _openai_response("ok")
        client = _async_client_with_response(payload)
        backend = LlamaCppBackend(_RESOLVER, client=client)
        await backend.complete([{"role": "user", "content": "hi"}], "m", temperature=0.5, max_tokens=100)
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["temperature"], 0.5)
        self.assertEqual(body["max_tokens"], 100)

    async def test_http_error_returns_error_result(self):
        client = _async_client_with_response({"error": "bad"}, status_code=503)
        backend = LlamaCppBackend(_RESOLVER, client=client)
        result = await backend.complete([{"role": "user", "content": "hi"}], "m")
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.finish_reason, "error")

    async def test_transport_error_returns_error_result(self):
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        backend = LlamaCppBackend(_RESOLVER, client=client)
        result = await backend.complete([{"role": "user", "content": "hi"}], "m")
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.error)

    async def test_url_constructed_correctly(self):
        payload = _openai_response("ok")
        client = _async_client_with_response(payload)
        resolver = lambda m: "http://localhost:8080/"  # noqa: E731
        backend = LlamaCppBackend(resolver, client=client)
        await backend.complete([], "m")
        posted_url = client.post.call_args.args[0]
        self.assertEqual(posted_url, "http://localhost:8080/v1/chat/completions")

    async def test_missing_url_raises_value_error(self):
        backend = LlamaCppBackend(lambda m: None)
        with self.assertRaises(ValueError):
            await backend.complete([], "unknown-model")

    def test_complete_sync_returns_result(self):
        payload = _openai_response("sync answer", model="llama3.1")
        sync_client = _sync_client_with_response(payload)
        backend = LlamaCppBackend(_RESOLVER, sync_client=sync_client)
        result = backend.complete_sync([{"role": "user", "content": "hi"}], "llama3.1")
        self.assertEqual(result.text, "sync answer")
        self.assertIsNone(result.error)

    def test_complete_sync_http_error_returns_error_result(self):
        sync_client = _sync_client_with_response({"error": "bad"}, status_code=503)
        backend = LlamaCppBackend(_RESOLVER, sync_client=sync_client)
        result = backend.complete_sync([], "m")
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.error)


class OpenRouterBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_completion(self):
        payload = _openai_response("remote answer", model="anthropic/claude-3-haiku")
        backend = OpenRouterBackend("sk-test", client=_async_client_with_response(payload))
        result = await backend.complete([{"role": "user", "content": "hi"}], "anthropic/claude-3-haiku")
        self.assertEqual(result.text, "remote answer")
        self.assertIsNone(result.error)

    async def test_http_error_returns_error_result(self):
        client = _async_client_with_response({"error": "unauthorized"}, status_code=401)
        backend = OpenRouterBackend("bad-key", client=client)
        result = await backend.complete([], "anthropic/claude-3-haiku")
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.error)

    async def test_transport_error_returns_error_result(self):
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        backend = OpenRouterBackend("sk-test", client=client)
        result = await backend.complete([], "anthropic/claude-3-haiku")
        self.assertIsNotNone(result.error)

    async def test_disabled_raises_runtime_error(self):
        backend = OpenRouterBackend(None)
        with self.assertRaises(RuntimeError) as ctx:
            await backend.complete([], "anthropic/claude-3-haiku")
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    async def test_url_hits_openrouter_endpoint(self):
        payload = _openai_response("ok")
        client = _async_client_with_response(payload)
        backend = OpenRouterBackend("sk-test", client=client)
        await backend.complete([], "anthropic/claude-3-haiku")
        posted_url = client.post.call_args.args[0]
        self.assertIn("openrouter.ai", posted_url)
        self.assertIn("chat/completions", posted_url)

    async def test_passes_kwargs_in_payload(self):
        payload = _openai_response("ok")
        client = _async_client_with_response(payload)
        backend = OpenRouterBackend("sk-test", client=client)
        await backend.complete([], "m", temperature=0.3, max_tokens=256)
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], 256)

    async def test_api_key_stored_on_backend(self):
        backend = OpenRouterBackend("sk-or-v1-abc123")
        self.assertEqual(backend._api_key, "sk-or-v1-abc123")
        self.assertFalse(backend._disabled)

    def test_stream_sync_disabled_raises(self):
        backend = OpenRouterBackend(None)
        with self.assertRaises(RuntimeError):
            with backend.stream_sync([], "m"):
                pass


class ModelClientRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(self) -> tuple[ModelClient, MagicMock, MagicMock]:
        llama_backend = MagicMock(spec=LlamaCppBackend)
        llama_backend.complete = AsyncMock(return_value=CompletionResult(
            text="local", model_used="llama3", tokens_in=1, tokens_out=2, finish_reason="stop"
        ))
        or_backend = MagicMock(spec=OpenRouterBackend)
        or_backend.complete = AsyncMock(return_value=CompletionResult(
            text="remote", model_used="claude", tokens_in=3, tokens_out=4, finish_reason="stop"
        ))
        return ModelClient(llama_backend, or_backend), llama_backend, or_backend

    async def test_local_prefix_routes_to_llama(self):
        client, llama, openrouter = self._make_client()
        result = await client.complete([{"role": "user", "content": "hi"}], "local:llama3.1")
        llama.complete.assert_called_once()
        openrouter.complete.assert_not_called()
        self.assertEqual(result.text, "local")

    async def test_local_prefix_stripped_before_forwarding(self):
        client, llama, _ = self._make_client()
        await client.complete([], "local:llama3.1")
        model_arg = llama.complete.call_args.args[1]
        self.assertEqual(model_arg, "llama3.1")

    async def test_openrouter_prefix_routes_to_openrouter(self):
        client, llama, openrouter = self._make_client()
        result = await client.complete([], "openrouter:anthropic/claude-3-haiku")
        openrouter.complete.assert_called_once()
        llama.complete.assert_not_called()
        self.assertEqual(result.text, "remote")

    async def test_openrouter_prefix_stripped_before_forwarding(self):
        client, _, openrouter = self._make_client()
        await client.complete([], "openrouter:anthropic/claude-3-haiku")
        model_arg = openrouter.complete.call_args.args[1]
        self.assertEqual(model_arg, "anthropic/claude-3-haiku")

    async def test_unknown_prefix_raises_value_error(self):
        client, _, _ = self._make_client()
        with self.assertRaises(ValueError):
            await client.complete([], "anthropic:claude-3")

    async def test_no_prefix_raises_value_error(self):
        client, _, _ = self._make_client()
        with self.assertRaises(ValueError):
            await client.complete([], "llama3.1")

    async def test_kwargs_forwarded_to_backend(self):
        client, llama, _ = self._make_client()
        await client.complete([], "local:m", temperature=0.7, max_tokens=512)
        _, kwargs = llama.complete.call_args
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["max_tokens"], 512)

    def test_stream_sync_local_routes_to_llama(self):
        client, llama, _ = self._make_client()
        llama.stream_sync = MagicMock()
        llama.stream_sync.return_value.__enter__ = MagicMock(return_value=MagicMock())
        llama.stream_sync.return_value.__exit__ = MagicMock(return_value=False)
        with client.stream_sync([], "local:llama3.1"):
            pass
        llama.stream_sync.assert_called_once()

    def test_stream_sync_openrouter_routes_to_openrouter(self):
        client, _, openrouter = self._make_client()
        openrouter.stream_sync = MagicMock()
        openrouter.stream_sync.return_value.__enter__ = MagicMock(return_value=MagicMock())
        openrouter.stream_sync.return_value.__exit__ = MagicMock(return_value=False)
        with client.stream_sync([], "openrouter:anthropic/claude-3-haiku"):
            pass
        openrouter.stream_sync.assert_called_once()

    def test_complete_sync_only_supports_local(self):
        client, _, _ = self._make_client()
        with self.assertRaises(ValueError):
            client.complete_sync([], "openrouter:anthropic/claude-3-haiku")


class BuildModelClientSingletonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import shared.model_client as _mc
        self._mc = _mc
        _mc._instance = None

    def tearDown(self):
        self._mc._instance = None

    def test_no_crash_without_api_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with patch("infra.config.get_model_url", return_value="http://localhost:8080"):
                with patch("infra.settings.get_openrouter_connection", return_value=None):
                    client = build_model_client()
        self.assertIsNotNone(client)

    def test_returns_same_instance(self):
        with patch("infra.config.get_model_url", return_value="http://localhost:8080"):
            with patch("infra.settings.get_openrouter_connection", return_value=None):
                a = build_model_client()
                b = build_model_client()
        self.assertIs(a, b)

    async def test_local_model_works_without_api_key(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        payload = _openai_response("hello", model="llama3")
        with patch("infra.config.get_model_url", return_value="http://localhost:8080"):
            with patch("infra.settings.get_openrouter_connection", return_value=None):
                mc = build_model_client()
        mc._llama._client = _async_client_with_response(payload)
        result = await mc.complete([{"role": "user", "content": "hi"}], "local:llama3")
        self.assertEqual(result.text, "hello")
        self.assertIsNone(result.error)

    async def test_openrouter_raises_without_api_key(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch("infra.config.get_model_url", return_value="http://localhost:8080"):
            with patch("infra.settings.get_openrouter_connection", return_value=None):
                mc = build_model_client()
        with self.assertRaises(RuntimeError) as ctx:
            await mc.complete([], "openrouter:anthropic/claude-3-haiku")
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))
