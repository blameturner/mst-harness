"""Handler contract stubs. Copy, rename, implement, then register at startup.

Registration (e.g. in app/lifespan.py or a dedicated setup module):

    from workers import kanban
    from workers.task_handlers import my_handler

    kanban.register("my_task_type", my_handler.handle, llm_bound=True)
"""
from workers.kanban import TaskHandler


# ── LLM-bound handler stub ───────────────────────────────────────────────────
# Register with llm_bound=True.
# Loop A ensures these run one at a time and only after the chat-idle grace period.

async def handle_llm(task: dict) -> dict:
    """
    task: full NocoDB row (keys match task_list schema).
    Returns: dict written to output_payload on success.
    Raise to trigger retry / failure handling.
    """
    _payload = task.get("input_payload") or {}
    raise NotImplementedError("replace with real implementation")


_llm_check: TaskHandler = handle_llm


# ── non-LLM handler stub ─────────────────────────────────────────────────────
# Register with llm_bound=False.
# Loop B may run up to N of these concurrently; no chat gate, no min-spacing.

async def handle_non_llm(task: dict) -> dict:
    _payload = task.get("input_payload") or {}
    raise NotImplementedError("replace with real implementation")


_non_llm_check: TaskHandler = handle_non_llm
