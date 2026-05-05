from __future__ import annotations

import contextvars
import logging
import threading
import time
from contextlib import contextmanager

from infra.config import MODELS, REASONER_ROLE, get_feature, get_function_config, no_think_params
from shared.model_pool import acquire_model, acquire_role

_log = logging.getLogger("workers.models")

DEFAULT_FAST_TIMEOUT = 900

_model_usage_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "_model_usage_ctx", default={}
)


def set_model_usage_context(
    *,
    org_id: int | None,
    source: str | None = None,
    conversation_id: int | None = None,
) -> None:
    try:
        org = int(org_id or 0)
    except Exception:
        org = 0
    current = _model_usage_ctx.get() or {}
    next_ctx = dict(current)
    next_ctx["org_id"] = org
    if source is not None:
        next_ctx["source"] = str(source)
    if conversation_id is not None:
        next_ctx["conversation_id"] = int(conversation_id or 0)
    _model_usage_ctx.set(next_ctx)


@contextmanager
def model_usage_scope(
    *,
    org_id: int | None,
    source: str | None = None,
    conversation_id: int | None = None,
):
    token = _model_usage_ctx.set(_model_usage_ctx.get() or {})
    try:
        set_model_usage_context(
            org_id=org_id,
            source=source,
            conversation_id=conversation_id,
        )
        yield
    finally:
        _model_usage_ctx.reset(token)


def _persist_model_usage_event(
    *,
    function_name: str,
    role: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_seconds: float,
    ok: bool,
) -> None:
    ctx = _model_usage_ctx.get() or {}
    org_id = int(ctx.get("org_id") or 0)
    if org_id <= 0:
        return

    source = (ctx.get("source") or "model_call").strip() or "model_call"
    conversation_id = int(ctx.get("conversation_id") or 0)
    status = "complete" if ok else "failed"
    task = f"{function_name}:{role}"

    def _writer() -> None:
        try:
            from infra.nocodb_client import NocodbClient

            db = NocodbClient()
            payload = {
                "org_id": org_id,
                "agent_name": function_name,
                "agent_version": 1,
                "product": source,
                "task_description": task[:500],
                "status": status,
                "tokens_input": int(prompt_tokens or 0),
                "tokens_output": int(completion_tokens or 0),
                "context_tokens": 0,
                "duration_seconds": float(duration_seconds or 0.0),
                "quality_score": 0,
                "model_name": str(model_name or role or "unknown"),
            }
            if conversation_id > 0:
                payload["summary"] = f"conversation_id={conversation_id}"
            db._post("agent_runs", payload)
        except Exception:
            _log.debug(
                "model usage persist skipped  fn=%s role=%s org=%s",
                function_name,
                role,
                org_id,
                exc_info=True,
            )

    threading.Thread(target=_writer, daemon=True).start()



# chat-only functions bypass the reasoner guard below
_CHAT_ONLY_FUNCTIONS = frozenset({"chat", "code"})


def _assert_not_reasoner(url: str | None, function_name: str) -> None:
    if function_name in _CHAT_ONLY_FUNCTIONS:
        return
    if not url:
        return
    reasoner_entry = MODELS.get(REASONER_ROLE)
    if not isinstance(reasoner_entry, dict):
        return
    reasoner_url = reasoner_entry.get("url")
    if reasoner_url and url == reasoner_url:
        raise RuntimeError(
            f"refusing to dispatch {function_name} call to reasoner "
            f"(url={url}). Non-chat functions must not use the reasoner."
        )


def _raw_model_call(
    label: str,
    function_name: str,
    role: str,
    url: str,
    model_id: str | None,
    prompt: str,
    max_tokens: int,
    temperature: float,
    extra_params: dict | None = None,
) -> tuple[str, int]:
    # slot acquisition is caller's responsibility
    started = time.time()
    _log.info(
        "%s start  url=%s model=%s prompt_len=%d max_tokens=%d",
        label, url, model_id, len(prompt), max_tokens,
    )
    try:
        from shared.model_client import LlamaCppBackend
        backend = LlamaCppBackend(url_resolver=lambda _: url)
        kwargs: dict = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            **no_think_params(model_id),
        }
        if extra_params:
            kwargs.update(extra_params)
        result = backend.complete_sync(
            messages=[{"role": "user", "content": prompt}],
            model=model_id or role,
            **kwargs,
        )
        elapsed = round(time.time() - started, 2)
        if result.error:
            _log.error("%s error: %s", label, result.error)
            _persist_model_usage_event(
                function_name=function_name, role=role,
                model_name=str(model_id or role),
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=elapsed, ok=False,
            )
            return "", 0
        tokens = result.tokens_in + result.tokens_out or (len(prompt) // 4 + max_tokens)
        _persist_model_usage_event(
            function_name=function_name,
            role=role,
            model_name=result.model_used,
            prompt_tokens=result.tokens_in,
            completion_tokens=result.tokens_out,
            duration_seconds=elapsed,
            ok=True,
        )
        _log.info("%s ok  tokens=%d %.2fs", label, tokens, elapsed)
        return result.text, tokens
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        _log.error("%s failed: %s", label, e)
        _persist_model_usage_event(
            function_name=function_name, role=role,
            model_name=str(model_id or role),
            prompt_tokens=0, completion_tokens=0,
            duration_seconds=elapsed, ok=False,
        )
        return "", 0


def model_call(
    function_name: str,
    prompt: str,
    priority: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> tuple[str, int]:
    cfg = get_function_config(function_name)
    role = cfg["role"]
    temp = temperature if temperature is not None else cfg.get("temperature", 0.2)
    mt = max_tokens if max_tokens is not None else cfg.get("max_tokens", 200)

    extra: dict = {}
    if cfg.get("frequency_penalty"):
        extra["frequency_penalty"] = cfg["frequency_penalty"]

    with acquire_role(role, priority=priority) as (url, model_id):
        if not url:
            _log.error("%s: no model for role=%s", function_name, role)
            return "", 0
        _assert_not_reasoner(url, function_name)
        return _raw_model_call(function_name, function_name, role, url, model_id, prompt, mt, temp, extra or None)


def _tool_call(prompt: str, max_tokens: int, temperature: float = 0.2) -> tuple[str, int]:
    with acquire_model("tool") as (url, model_id):
        if not url:
            return "", 0
        return _raw_model_call("tool_call", "tool_call", "tool", url, model_id, prompt, max_tokens, temperature)


def _fast_call(prompt: str, max_tokens: int, temperature: float = 0.2) -> tuple[str, int]:
    with acquire_model("fast") as (url, model_id):
        if not url:
            return "", 0
        return _raw_model_call("fast_call", "fast_call", "fast", url, model_id, prompt, max_tokens, temperature)
