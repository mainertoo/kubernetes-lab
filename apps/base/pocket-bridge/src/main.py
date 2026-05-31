"""Pocket-bridge entrypoint — three uvicorn ports + recovery tasks.

Plan §3 architecture + D15 (three-port bridge). Starts uvicorn on
:8080 (webhook), :8081 (admin/healthz), :8082 (metrics) under one
asyncio loop. On startup runs §7.8 Layer A recovery scan before
serving any traffic; in parallel kicks off §7.8 Layer B periodic
stale scanner + Open Notebook commands probe.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn
from fastapi import FastAPI
from prometheus_client import make_asgi_app

import config
import metrics as M
from admin import build_admin_app
from open_notebook import OpenNotebookClient
from pocket import PocketAPIClient
from poller import (
    open_notebook_stale_commands_probe,
    periodic_stale_scanner,
    start_recovery_scan,
)
from state import StateMachine, make_redis
from webhook import build_webhook_app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pocket-bridge")


async def _refresh_health(sm: StateMachine, on: OpenNotebookClient) -> None:
    """Periodically refresh redis_up / open_notebook_up gauges."""
    while True:
        M.redis_up.set(1 if await sm.ping() else 0)
        try:
            ok = await on.ping()
            M.open_notebook_up.set(1 if ok else 0)
            M.open_notebook_ping_total.labels(result="success" if ok else "fail").inc()
        except Exception:
            M.open_notebook_up.set(0)
            M.open_notebook_ping_total.labels(result="fail").inc()
        await asyncio.sleep(30)


def _make_metrics_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/metrics", make_asgi_app())
    return app


async def _serve(app: FastAPI, port: int) -> None:
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")  # noqa: S104
    server = uvicorn.Server(cfg)
    await server.serve()


async def amain() -> None:
    cfg = config.load()
    log.info(
        "starting pocket-bridge: webhook=:%d admin=:%d metrics=:%d ON=%s redis=%s:%d",
        cfg.webhook_port, cfg.admin_port, cfg.metrics_port,
        cfg.open_notebook_base_url, cfg.redis_host, cfg.redis_port,
    )

    r = await make_redis(cfg)
    sm = StateMachine(cfg, r)
    await sm.setup()

    on = OpenNotebookClient(cfg)
    pocket = PocketAPIClient(cfg)

    webhook_app = build_webhook_app(cfg=cfg, sm=sm, on=on)
    admin_app = build_admin_app(cfg=cfg, sm=sm, on=on, pocket=pocket)
    metrics_app = _make_metrics_app()

    # §7.8 Layer A — run BEFORE serving traffic so re-dispatched pollers start before
    # any new webhook can land
    await start_recovery_scan(cfg=cfg, sm=sm, on=on)

    tasks = [
        asyncio.create_task(_serve(webhook_app, cfg.webhook_port)),
        asyncio.create_task(_serve(admin_app, cfg.admin_port)),
        asyncio.create_task(_serve(metrics_app, cfg.metrics_port)),
        # §7.8 Layer B + commands probe
        asyncio.create_task(periodic_stale_scanner(cfg=cfg, sm=sm, on=on)),
        asyncio.create_task(open_notebook_stale_commands_probe(on=on)),
        asyncio.create_task(_refresh_health(sm, on)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    log.info("shutdown signal received; stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await on.aclose()
    await pocket.aclose()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
