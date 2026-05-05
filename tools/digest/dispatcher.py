from __future__ import annotations

import logging

from infra.config import is_feature_enabled
from infra.nocodb_client import NocodbClient
from tools._org import default_org_id

_log = logging.getLogger("digest.dispatcher")


def jumpstart_daily_digest(org_id: int | None = None) -> dict:
    """Enqueue at most one `daily_digest` job per tick, gated by inflight count."""
    if not is_feature_enabled("daily_digest"):
        return {"status": "disabled"}

    from workers import kanban

    client = NocodbClient()
    inflight = kanban.count_inflight(client, "daily_digest")
    if inflight > 0:
        return {"status": "already_running", "inflight": inflight}

    if org_id is not None:
        org_id = int(org_id)
    if not org_id:
        org_id = default_org_id(client)
    if not org_id:
        _log.info("daily_digest dispatcher: no_org_context")
        return {"status": "no_org_context"}

    try:
        task_id = kanban.submit(
            client,
            "daily_digest",
            {"org_id": org_id},
            created_by="daily_digest_jumpstart",
        )
    except Exception:
        _log.warning("daily_digest submit failed", exc_info=True)
        return {"status": "submit_failed"}
    _log.info("daily_digest queued task_id=%d org_id=%d", task_id, org_id)
    return {"status": "kicked", "org_id": org_id}
