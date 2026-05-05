from __future__ import annotations

import logging

from infra.config import is_feature_enabled
from infra.nocodb_client import NocodbClient
from tools._org import default_org_id

_log = logging.getLogger("seed_feedback.dispatcher")


def jumpstart_seed_feedback(org_id: int | None = None) -> dict:
    """Insert at most one `seed_feedback` Kanban task per tick."""
    if not is_feature_enabled("seed_feedback"):
        return {"status": "disabled"}

    from workers import kanban

    client = NocodbClient()
    inflight = kanban.count_inflight(client, "seed_feedback")
    if inflight > 0:
        return {"status": "already_running", "inflight": inflight}

    if org_id is not None:
        org_id = int(org_id)
    if not org_id:
        org_id = default_org_id(client)
    if not org_id:
        _log.info("seed_feedback dispatcher: no_org_context")
        return {"status": "no_org_context"}

    try:
        row_id = kanban.submit(
            client,
            "seed_feedback",
            {"org_id": org_id},
            created_by="seed_feedback_jumpstart",
        )
    except Exception:
        _log.warning("seed_feedback submit failed", exc_info=True)
        return {"status": "submit_failed"}
    _log.info("seed_feedback queued row_id=%d org_id=%d", row_id, org_id)
    return {"status": "kicked", "org_id": org_id}
