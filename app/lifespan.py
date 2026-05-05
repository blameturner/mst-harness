from contextlib import asynccontextmanager

from fastapi import FastAPI

import infra.log as log

log.setup()
_log = log.get("harness")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("mstag-harness starting")
    from infra.config import HUEY_ENABLED
    from infra.huey_runtime import init_huey, shutdown_huey, start_huey_consumer

    if not HUEY_ENABLED:
        raise RuntimeError("HUEY_ENABLED is false; tool queue is configured for Huey-only execution")

    huey = init_huey()
    app.state.huey = huey
    # SqliteHuey is falsy when the queue is empty; only treat None as init failure.
    if huey is None:
        raise RuntimeError("Huey initialisation failed; refusing startup in Huey-only mode")
    started = start_huey_consumer()
    if not started:
        raise RuntimeError("Huey consumer failed to start; refusing startup in Huey-only mode")
    _log.info("huey runtime ready  consumer_started=%s", started)

    from scheduler import start_scheduler
    sched = start_scheduler()
    app.state.scheduler = sched
    _log.info("scheduler running")

    from workers.tool_queue import HandlerConfig, ToolJobQueue, _set_instance
    from tools.simulation.agent import run_simulation_job
    tool_queue = ToolJobQueue()
    # Harvest pipeline — generic scraper/pathfinder-driven jobs.
    # Importing the package self-registers all policies in tools.harvest.REGISTRY.
    # max_workers=1 because each harvest already drives a long sequential
    # per-URL LLM loop on a single local CPU model; running 2 concurrently
    # just thrashes the model pool and starves chat / cron jobs that share
    # the same LLM slot.
    from tools.harvest import run_harvest
    tool_queue.register("harvest_run", HandlerConfig(
        handler=lambda p: run_harvest(p["run_id"]),
        max_workers=1, priority_default=4, source="harvest",
    ))
    tool_queue.register("simulation_run", HandlerConfig(
        handler=run_simulation_job,
        max_workers=1, priority_default=4, source="simulation",
    ))
    _set_instance(tool_queue)
    app.state.tool_queue = tool_queue
    tool_queue.start()
    _log.info("tool job queue running")

    import asyncio as _asyncio
    from workers import kanban as _kanban
    from workers.task_handlers import scrape_page as _scrape_page_handler
    from workers.task_handlers import corpus_maintenance as _corpus_maintenance_handler
    from workers.task_handlers import graph_maintenance as _graph_maintenance_handler
    from workers.task_handlers import seed_feedback as _seed_feedback_handler
    from workers.task_handlers import research_planner as _research_planner_handler
    from workers.task_handlers import research_agent as _research_agent_handler
    from workers.task_handlers import research_review as _research_review_handler
    from workers.task_handlers import research_op as _research_op_handler
    from workers.task_handlers import graph_extract as _graph_extract_handler
    from workers.task_handlers import summarise_page as _summarise_page_handler
    from workers.task_handlers import extract_relationships as _extract_relationships_handler
    from workers.task_handlers import discover_agent_run as _discover_agent_handler
    from workers.task_handlers import insight_produce as _insight_produce_handler
    from workers.task_handlers import pa_topic_research as _pa_topic_research_handler
    from workers.task_handlers import daily_digest as _daily_digest_handler
    from workers.task_handlers import pathfinder_extract as _pathfinder_extract_handler
    from workers.task_handlers import graph_resolve_entities as _graph_resolve_entities_handler
    from infra.nocodb_client import NocodbClient as _NocodbClient
    _kanban.register("scrape_page", _scrape_page_handler.handle, llm_bound=False)
    _kanban.register("corpus_maintenance", _corpus_maintenance_handler.handle, llm_bound=False)
    _kanban.register("graph_maintenance", _graph_maintenance_handler.handle, llm_bound=False)
    _kanban.register("seed_feedback", _seed_feedback_handler.handle, llm_bound=False)
    _kanban.register("research_planner", _research_planner_handler.handle, llm_bound=True)
    _kanban.register("research_agent", _research_agent_handler.handle, llm_bound=True)
    _kanban.register("research_review", _research_review_handler.handle, llm_bound=True)
    _kanban.register("research_op", _research_op_handler.handle, llm_bound=True)
    _kanban.register("graph_extract", _graph_extract_handler.handle, llm_bound=True)
    _kanban.register("summarise_page", _summarise_page_handler.handle, llm_bound=True)
    _kanban.register("extract_relationships", _extract_relationships_handler.handle, llm_bound=True)
    _kanban.register("discover_agent_run", _discover_agent_handler.handle, llm_bound=True)
    _kanban.register("insight_produce", _insight_produce_handler.handle, llm_bound=True)
    _kanban.register("pa_topic_research", _pa_topic_research_handler.handle, llm_bound=True)
    _kanban.register("daily_digest", _daily_digest_handler.handle, llm_bound=True)
    _kanban.register("pathfinder_extract", _pathfinder_extract_handler.handle, llm_bound=False)
    _kanban.register("graph_resolve_entities", _graph_resolve_entities_handler.handle, llm_bound=True)
    _kanban_db = _NocodbClient()
    _kanban_llm_task = _asyncio.create_task(_kanban.run_llm_loop(_kanban_db), name="kanban-llm")
    _kanban_non_llm_task = _asyncio.create_task(_kanban.run_non_llm_loop(_kanban_db), name="kanban-non-llm")
    _log.info("kanban loops started")

    # Periodic dispatchers. Each one enqueues at most one job per tick and is
    # guarded by an inflight check. The single chat-idle gate in tool_queue
    # decides when a job actually runs — no startup delay needed here.
    try:
        from infra.config import get_feature
        from tools.enrichment.dispatcher import (
            jumpstart_discover_agent,
            jumpstart_pathfinder,
            jumpstart_scraper,
        )
        from tools.digest.dispatcher import jumpstart_daily_digest
        from tools.seed_feedback.dispatcher import jumpstart_seed_feedback
        from tools.corpus_maintenance.dispatcher import jumpstart_corpus_maintenance
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scrape_seconds = int(get_feature("scraper", "dispatch_interval_seconds", 60))
        sched.add_job(
            jumpstart_scraper,
            IntervalTrigger(seconds=max(15, scrape_seconds)),
            id="enrichment_scrape_dispatcher",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        pathfinder_seconds = int(get_feature("pathfinder", "dispatch_interval_seconds", 120))
        sched.add_job(
            jumpstart_pathfinder,
            IntervalTrigger(seconds=max(30, pathfinder_seconds)),
            id="pathfinder_dispatcher",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        discover_minutes = int(get_feature("discover_agent", "run_interval_minutes", 20))
        sched.add_job(
            jumpstart_discover_agent,
            IntervalTrigger(minutes=max(1, discover_minutes)),
            id="discover_agent_dispatcher",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        _log.info(
            "enrichment dispatchers scheduled  scrape=%ds pathfinder=%ds discover=%dm",
            scrape_seconds, pathfinder_seconds, discover_minutes,
        )

        if get_feature("daily_digest", "enabled", True):
            digest_hour = int(get_feature("daily_digest", "cron_hour", 7))
            digest_minute = int(get_feature("daily_digest", "cron_minute", 0))
            sched.add_job(
                jumpstart_daily_digest,
                CronTrigger(hour=digest_hour, minute=digest_minute),
                id="daily_digest_dispatcher",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            _log.info("daily_digest dispatcher scheduled  %02d:%02d UTC", digest_hour, digest_minute)

        from tools.research.research_planner import reap_stale_plans
        reap_minutes = int(get_feature("research", "reap_interval_minutes", 30) or 30)
        sched.add_job(
            reap_stale_plans,
            IntervalTrigger(minutes=max(5, reap_minutes)),
            id="research_plan_reaper",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        _log.info("research plan reaper scheduled  every=%dm", reap_minutes)

        if get_feature("seed_feedback", "enabled", True):
            seed_hours = int(get_feature("seed_feedback", "run_interval_hours", 6))
            sched.add_job(
                jumpstart_seed_feedback,
                IntervalTrigger(hours=max(1, seed_hours)),
                id="seed_feedback_dispatcher",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            _log.info("seed_feedback dispatcher scheduled  every=%dh", seed_hours)

        if get_feature("graph_maintenance", "enabled", True):
            er_hours = int(get_feature("graph_maintenance", "entity_resolution_interval_hours", 24))
            gm_hours = int(get_feature("graph_maintenance", "maintenance_interval_hours", 168))
            from tools.graph_maintenance.dispatcher import (
                jumpstart_entity_resolution,
                jumpstart_graph_maintenance,
            )
            sched.add_job(
                jumpstart_entity_resolution,
                IntervalTrigger(hours=max(1, er_hours)),
                id="graph_entity_resolution_dispatcher",
                max_instances=1, coalesce=True, replace_existing=True,
            )
            sched.add_job(
                jumpstart_graph_maintenance,
                IntervalTrigger(hours=max(1, gm_hours)),
                id="graph_maintenance_dispatcher",
                max_instances=1, coalesce=True, replace_existing=True,
            )
            _log.info("graph maintenance scheduled  entity_res=%dh maintenance=%dh",
                      er_hours, gm_hours)

        if get_feature("insights", "enabled", True):
            insight_tick_minutes = int(get_feature("insights", "tick_minutes", 10))
            from tools.insight.dispatcher import jumpstart_insights
            sched.add_job(
                jumpstart_insights,
                IntervalTrigger(minutes=max(1, insight_tick_minutes)),
                id="insight_dispatcher",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            _log.info("insight dispatcher scheduled  tick=%dm", insight_tick_minutes)

        if get_feature("corpus_maintenance", "enabled", True):
            maint_hours = int(get_feature("corpus_maintenance", "run_interval_hours", 12))
            sched.add_job(
                jumpstart_corpus_maintenance,
                IntervalTrigger(hours=max(1, maint_hours)),
                id="corpus_maintenance_dispatcher",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            _log.info("corpus_maintenance dispatcher scheduled  every=%dh", maint_hours)
    except Exception:
        _log.error("enrichment dispatcher registration failed", exc_info=True)

    _log.info("ready")
    try:
        yield
    finally:
        tool_queue.stop()
        _kanban_llm_task.cancel()
        _kanban_non_llm_task.cancel()
        await _asyncio.gather(_kanban_llm_task, _kanban_non_llm_task, return_exceptions=True)
        from shared.model_client import close_model_client
        await close_model_client()
        shutdown_huey()
        sched.shutdown(wait=False)
        _log.info("shutdown complete")
