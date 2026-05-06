from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

_log = logging.getLogger(__name__)

_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 900.0


@dataclass
class CompletionResult:
    text: str
    model_used: str
    tokens_in: int
    tokens_out: int
    finish_reason: str
    error: str | None = field(default=None)


class LlamaCppBackend:
    def __init__(
        self,
        url_resolver: Callable[[str], str | None],
        *,
        client: httpx.AsyncClient | None = None,
        sync_client: httpx.Client | None = None,
    ) -> None:
        self._resolve = url_resolver
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        self._sync_client = sync_client or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def _url_for(self, model: str) -> str:
        base = self._resolve(model)
        if not base:
            raise ValueError(f"no URL found for local model {model!r}")
        return f"{base.rstrip('/')}/v1/chat/completions"

    async def complete(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        url = self._url_for(model)
        payload = {"model": model, "messages": messages, **kwargs}
        try:
            r = await self._client.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log.error("llama.cpp %d %s", exc.response.status_code, exc.response.text[:200])
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        except httpx.TransportError as exc:
            _log.error("llama.cpp transport error: %s", exc)
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        return _parse_openai_response(r.json(), model)

    def complete_sync(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        url = self._url_for(model)
        payload = {"model": model, "messages": messages, **kwargs}
        try:
            r = self._sync_client.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log.error("llama.cpp %d %s", exc.response.status_code, exc.response.text[:200])
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        except httpx.TransportError as exc:
            _log.error("llama.cpp transport error: %s", exc)
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        return _parse_openai_response(r.json(), model)

    @contextmanager
    def stream_sync(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> Generator[httpx.Response, None, None]:
        """Context manager yielding a streaming httpx.Response.

        Caller iterates response.iter_lines() for raw SSE lines.
        """
        url = self._url_for(model)
        payload = {"model": model, "messages": messages, "stream": True, **kwargs}
        with self._sync_client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            yield response

    async def aclose(self) -> None:
        await self._client.aclose()
        self._sync_client.close()


class OpenRouterBackend:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = _OPENROUTER_BASE_URL,
        *,
        client: httpx.AsyncClient | None = None,
        sync_client: httpx.Client | None = None,
    ) -> None:
        self._disabled = not api_key
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self._api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, headers=headers)
        self._sync_client = sync_client or httpx.Client(timeout=_DEFAULT_TIMEOUT, headers=headers)

    async def complete(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        if self._disabled:
            raise RuntimeError(
                f"OpenRouter model requested but {_OPENROUTER_API_KEY_ENV} env var is not set"
            )
        payload = {"model": model, "messages": messages, **kwargs}
        url = f"{self._base_url}/chat/completions"
        try:
            r = await self._client.post(url, json=payload)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log.error("openrouter %d %s", exc.response.status_code, exc.response.text[:200])
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        except httpx.TransportError as exc:
            _log.error("openrouter transport error: %s", exc)
            return CompletionResult(
                text="", model_used=model, tokens_in=0, tokens_out=0,
                finish_reason="error", error=str(exc),
            )
        return _parse_openai_response(r.json(), model)

    @contextmanager
    def stream_sync(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> Generator[httpx.Response, None, None]:
        if self._disabled:
            raise RuntimeError(
                f"OpenRouter model requested but {_OPENROUTER_API_KEY_ENV} env var is not set"
            )
        url = f"{self._base_url}/chat/completions"
        payload = {"model": model, "messages": messages, "stream": True, **kwargs}
        with self._sync_client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            yield response

    async def aclose(self) -> None:
        await self._client.aclose()
        self._sync_client.close()


class ModelClient:
    def __init__(self, llama_cpp: LlamaCppBackend, openrouter: OpenRouterBackend) -> None:
        self._llama = llama_cpp
        self._openrouter = openrouter

    async def complete(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        if model.startswith("local:"):
            return await self._llama.complete(messages, model[len("local:"):], **kwargs)
        if model.startswith("openrouter:"):
            return await self._openrouter.complete(messages, model[len("openrouter:"):], **kwargs)
        raise ValueError(
            f"unrecognised model prefix in {model!r}; expected 'local:' or 'openrouter:'"
        )

    def complete_sync(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        if model.startswith("local:"):
            return self._llama.complete_sync(messages, model[len("local:"):], **kwargs)
        raise ValueError(
            f"complete_sync only supports 'local:' models; got {model!r}"
        )

    @contextmanager
    def stream_sync(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> Generator[httpx.Response, None, None]:
        if model.startswith("local:"):
            with self._llama.stream_sync(messages, model[len("local:"):], **kwargs) as resp:
                yield resp
            return
        if model.startswith("openrouter:"):
            with self._openrouter.stream_sync(messages, model[len("openrouter:"):], **kwargs) as resp:
                yield resp
            return
        raise ValueError(
            f"stream_sync unrecognised model prefix in {model!r}; expected 'local:' or 'openrouter:'"
        )

    async def aclose(self) -> None:
        await self._llama.aclose()
        await self._openrouter.aclose()


_instance: ModelClient | None = None
_instance_lock = threading.Lock()


def build_model_client() -> ModelClient:
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        from infra.config import get_model_url  # lazy — config init not guaranteed at import time
        from infra.settings import get_openrouter_connection
        conn = get_openrouter_connection()
        api_key = (conn or {}).get("api_key") or os.environ.get(_OPENROUTER_API_KEY_ENV, "").strip() or None
        base_url = (conn or {}).get("base_url") or _OPENROUTER_BASE_URL
        _instance = ModelClient(
            llama_cpp=LlamaCppBackend(url_resolver=get_model_url),
            openrouter=OpenRouterBackend(api_key, base_url),
        )
        return _instance


def reset_model_client() -> None:
    """Drop the singleton so the next call to build_model_client() re-reads config.

    Schedules aclose() on the old client if an event loop is running so the
    underlying httpx connection pool is not leaked. If no loop is running (e.g.
    called from a test or startup path) the client is simply dropped and GC will
    emit a ResourceWarning — callers there should use close_model_client() instead.
    """
    global _instance
    with _instance_lock:
        old, _instance = _instance, None
    if old is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(old.aclose())
        except RuntimeError:
            pass  # no running loop; caller is responsible for cleanup


async def close_model_client() -> None:
    global _instance
    if _instance is None:
        return
    await _instance.aclose()
    _instance = None


def _parse_openai_response(data: dict, requested_model: str) -> CompletionResult:
    try:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        text = (msg.get("content") or "").strip()
        # some reasoning models return no content but populate reasoning_content
        if not text:
            text = (msg.get("reasoning_content") or "").strip()
        finish_reason = choice.get("finish_reason") or "unknown"
    except (KeyError, IndexError) as exc:
        return CompletionResult(
            text="", model_used=requested_model, tokens_in=0, tokens_out=0,
            finish_reason="error", error=f"malformed response: {exc}",
        )
    usage = data.get("usage") or {}
    return CompletionResult(
        text=text,
        model_used=data.get("model") or requested_model,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        finish_reason=finish_reason,
    )
