from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

_log = logging.getLogger(__name__)

_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 900.0
_ALLOWLIST_TTL_S = 24 * 3600

# Conservative fallback used when the OpenRouter /models fetch fails at startup.
_FREE_TIER_FALLBACK: frozenset[str] = frozenset(
    {
        "google/gemma-3n-e4b-it:free",
        "meta-llama/llama-3.1-8b-instruct:free",
        "meta-llama/llama-3.2-3b-instruct:free",
        "microsoft/phi-3-mini-128k-instruct:free",
        "mistralai/mistral-7b-instruct:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
        "qwen/qwen-2-7b-instruct:free",
    }
)


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
        *,
        client: httpx.AsyncClient | None = None,
        # Inject a fixed allowlist to skip the fetch entirely (tests only).
        _allowlist: frozenset[str] | None = None,
    ) -> None:
        self._disabled = not api_key
        self._api_key = api_key or ""
        headers = {"Authorization": f"Bearer {self._api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, headers=headers)
        self._allowlist: frozenset[str] = _allowlist or frozenset()
        # Far-future sentinel means "never refetch" when injected; 0 means "fetch on first use".
        self._allowlist_fetched_at: float = float("inf") if _allowlist is not None else 0.0
        self._allowlist_lock: asyncio.Lock = asyncio.Lock()

    async def complete(
        self, messages: list[dict], model: str, **kwargs: object
    ) -> CompletionResult:
        if self._disabled:
            raise RuntimeError(
                f"OpenRouter model requested but {_OPENROUTER_API_KEY_ENV} env var is not set"
            )
        await self._ensure_allowlist()
        if model not in self._allowlist:
            raise ValueError(
                f"model {model!r} is not on the OpenRouter free tier; "
                f"use a ':free' model or one priced at $0 for prompt and completion"
            )
        payload = {"model": model, "messages": messages, **kwargs}
        url = f"{_OPENROUTER_BASE_URL}/chat/completions"
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

    async def _ensure_allowlist(self) -> None:
        if time.monotonic() - self._allowlist_fetched_at < _ALLOWLIST_TTL_S:
            return
        async with self._allowlist_lock:
            if time.monotonic() - self._allowlist_fetched_at < _ALLOWLIST_TTL_S:
                return  # another coroutine refreshed while we waited
            fetched = await self._fetch_allowlist()
            self._allowlist = fetched
            self._allowlist_fetched_at = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch_allowlist(self) -> frozenset[str]:
        try:
            r = await self._client.get(f"{_OPENROUTER_BASE_URL}/models")
            r.raise_for_status()
            models: list[dict] = r.json().get("data") or []
            result = frozenset(m["id"] for m in models if _is_free_model(m))
            _log.info("openrouter allowlist refreshed  count=%d", len(result))
            return result
        except Exception as exc:
            _log.warning(
                "openrouter allowlist fetch failed (%s); using hardcoded fallback", exc
            )
            return _FREE_TIER_FALLBACK


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
        raise ValueError(
            f"stream_sync only supports 'local:' models; got {model!r}"
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
        api_key = os.environ.get(_OPENROUTER_API_KEY_ENV, "").strip() or None
        _instance = ModelClient(
            llama_cpp=LlamaCppBackend(url_resolver=get_model_url),
            openrouter=OpenRouterBackend(api_key),
        )
        return _instance


async def close_model_client() -> None:
    global _instance
    if _instance is None:
        return
    await _instance.aclose()
    _instance = None


def _is_free_model(model_data: dict) -> bool:
    if str(model_data.get("id", "")).endswith(":free"):
        return True
    pricing = model_data.get("pricing") or {}
    return str(pricing.get("prompt", "1")) == "0" and str(pricing.get("completion", "1")) == "0"


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
