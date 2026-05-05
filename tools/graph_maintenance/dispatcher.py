"""APScheduler ticks for graph maintenance jobs.

Lightweight — just enqueues onto the tool queue. Heavy lifting is in
``tools.graph_maintenance.agent``.
"""
from __future__ import annotations

import logging

from infra.config import is_feature_enabled
from tools._org import default_org_id, resolve_org_id

_log = logging.getLogger("graph.maintenance.dispatcher")


def jumpstart_entity_resolution(org_id: int | None = None) -> dict:
    if not is_feature_enabled("graph_maintenance"):
        return {"status": "disabled"}
    try:
        from infra.nocodb_client import NocodbClient
        from workers import kanban
        client = NocodbClient()
        org = resolve_org_id(org_id or default_org_id(client))
    except Exception:
        _log.warning("graph_resolve_entities dispatcher: setup failed", exc_info=True)
        return {"status": "error"}
    if not org:
        return {"status": "no_org"}
    try:
        task_id = kanban.submit(client, "graph_resolve_entities", {"org_id": org},
                                created_by="graph_maintenance_dispatcher")
        _log.info("graph_resolve_entities queued  task_id=%d org=%d", task_id, org)
        return {"status": "queued", "task_id": task_id, "org_id": org}
    except Exception as e:
        _log.warning("graph_resolve_entities submit failed  err=%s", e, exc_info=True)
        return {"status": "submit_failed", "error": str(e)}


def jumpstart_graph_maintenance(org_id: int | None = None) -> dict:
    if not is_feature_enabled("graph_maintenance"):
        return {"status": "disabled"}
    try:
        from infra.nocodb_client import NocodbClient
        from workers import kanban
        client = NocodbClient()
        org = resolve_org_id(org_id or default_org_id(client))
    except Exception:
        _log.warning("graph_maintenance dispatcher: setup failed", exc_info=True)
        return {"status": "error"}
    if not org:
        return {"status": "no_org"}
    try:
        row_id = kanban.submit(client, "graph_maintenance", {"org_id": org},
                               created_by="graph_maintenance_dispatcher")
        _log.info("graph_maintenance queued  row_id=%d org=%d", row_id, org)
        return {"status": "queued", "task_id": row_id, "org_id": org}
    except Exception as e:
        _log.warning("graph_maintenance submit failed  err=%s", e, exc_info=True)
        return {"status": "submit_failed", "error": str(e)}
