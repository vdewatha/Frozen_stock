from __future__ import annotations

from datetime import datetime, time as clock_time, timezone
import os
import threading
import time

print("FastAPI module import started", flush=True)


def _import_stage(name: str) -> None:
    print(f"FastAPI import stage complete: {name}", flush=True)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
_import_stage("fastapi")

from app.core.config import settings
_import_stage("config")
from app.core.security import AuthenticationMiddleware
_import_stage("security")

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(AuthenticationMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_routers_ready = False
_scalp_routes_ready = False
_router_error: str | None = None


@app.middleware("http")
async def wait_for_application_routes(request: Request, call_next):
    """Keep hosted boot truthful while the large router graph loads."""
    if request.url.path == "/api/health":
        return await call_next(request)
    scalp_path = request.url.path.startswith("/api/research/scalp")
    iex_status_path = request.url.path == "/api/market-data/iex/status"
    if request.url.path.startswith("/api/") and not _routers_ready and not (
        (scalp_path and _scalp_routes_ready) or iex_status_path
    ):
        body = {
            "status": "starting",
            "detail": "Paper API routes are still loading",
            "paper_only": True,
            "live_trading": False,
        }
        if _router_error:
            body["error"] = _router_error
        return JSONResponse(status_code=503, content=body)
    return await call_next(request)


@app.get("/api/health")
def hosted_health() -> dict:
    """Lightweight liveness endpoint that never waits on research providers."""
    return {
        "status": "ok",
        "service": "paper-trading-api",
        "paper_only": True,
        "live_trading": False,
        "scalp_routes_ready": _scalp_routes_ready,
        # Readiness must cover the core router graph as well as the lightweight
        # scalp lane.  The gateway uses this field to decide whether hosted
        # paper routes can be served, so reporting scalp-only readiness would
        # make delayed-SIP and paper endpoints look live while still returning
        # the route-loader's 503 response.
        "routes_ready": _routers_ready and _scalp_routes_ready,
        "all_routes_ready": _routers_ready,
        **({"route_error": _router_error} if _router_error else {}),
    }


@app.get("/api/market-data/iex/status")
def hosted_iex_status():
    """Expose the read-only IEX research status during slow hosted startup."""
    from app.db.session import SessionLocal
    from app.services.alpaca_research_data import iex_research_status

    db = SessionLocal()
    try:
        return iex_research_status(db)
    finally:
        db.close()


def load_application_routes() -> None:
    """Register the core API, then load optional research routers separately."""
    global _router_error, _routers_ready
    try:
        from app.api.routes import router
        _import_stage("routes")

        # The core router owns health, readiness, paper controls, and the
        # activity views. Register it before optional provider integrations so
        # a slow research import cannot hold the whole API in 503 startup.
        app.include_router(router, prefix="/api")
        _routers_ready = True
        print("FastAPI core routes ready", flush=True)

        if settings.environment == "production":
            threading.Thread(
                target=load_optional_application_routes,
                name="load-optional-api-routes",
                daemon=True,
            ).start()
        else:
            load_optional_application_routes()
    except Exception as exc:  # pragma: no cover - hosted import diagnostics
        _router_error = exc.__class__.__name__
        print(f"FastAPI core route loading failed: {_router_error}", flush=True)


def load_optional_application_routes() -> None:
    """Load provider-specific and research routers without blocking liveness."""
    global _router_error
    try:
        from app.api.research import router as research_router
        _import_stage("research")
        from app.api.agent_research import router as agent_research_router
        _import_stage("agent_research")
        _import_stage("scalp_research")
        from app.api.crypto_research import router as crypto_router
        _import_stage("crypto_research")
        from app.api.shadow_evidence import router as shadow_evidence_router
        _import_stage("shadow_evidence")
        from app.api.stock_paper import router as stock_paper_router
        _import_stage("stock_paper")
        from app.api.stock_training import router as stock_training_router
        _import_stage("stock_training")
        from app.api.stock_forward_trial import router as stock_forward_trial_router
        _import_stage("stock_forward_trial")
        from app.api.stock_learning_cycle import router as stock_learning_cycle_router
        _import_stage("stock_learning_cycle")
        from app.api.live_broker import router as live_broker_router
        _import_stage("live_broker")
        from app.api.live_pilot import router as live_pilot_router
        _import_stage("live_pilot")

        for application_router in (
            research_router,
            agent_research_router,
            crypto_router,
            shadow_evidence_router,
            stock_paper_router,
            stock_training_router,
            stock_forward_trial_router,
            stock_learning_cycle_router,
            live_broker_router,
            live_pilot_router,
        ):
            app.include_router(application_router, prefix="/api")
        print("FastAPI optional routes ready", flush=True)
    except Exception as exc:  # pragma: no cover - hosted import diagnostics
        _router_error = exc.__class__.__name__
        print(f"FastAPI optional route loading failed: {_router_error}", flush=True)


def load_scalp_routes() -> None:
    """Retained for compatibility with local startup diagnostics."""
    global _scalp_routes_ready
    _scalp_routes_ready = True


def _run_direct_iex_collection_once() -> None:
    """Run one coordinated IEX observation without enabling paper workers.

    Autoscale deployments do not provide a durable Celery worker process. This
    narrow loop keeps only the research feed alive when explicitly enabled;
    Redis coordination prevents duplicate collections across API instances.
    It never imports an execution task or grants trading eligibility.
    """
    try:
        from app.services.iex_research_runner import run_iex_collection_once

        result = run_iex_collection_once(now=datetime.now(timezone.utc))
        print(
            "Direct IEX research collection complete: "
            f"status={result.get('status')} symbols={len(result.get('results', []))}",
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - hosted operational path
        # Keep the API alive and preserve the fail-closed readiness state.
        print(f"Direct IEX research collection unavailable: {exc.__class__.__name__}", flush=True)


def _direct_iex_research_loop() -> None:
    from app.services.iex_research_runner import run_iex_research_loop

    run_iex_research_loop()


def _run_direct_scalp_research_once(symbol: str) -> None:
    """Run one leased, paper-only scalp research job for one symbol."""
    try:
        from app.services.scalp_research_runner import run_scalp_research_once

        result = run_scalp_research_once(symbol, now=datetime.now(timezone.utc))
        print(f"Direct scalp research complete: symbol={symbol} status={result.get('status')}", flush=True)
    except Exception as exc:  # pragma: no cover - hosted operational path
        print(
            f"Direct scalp research unavailable: symbol={symbol} error={exc.__class__.__name__}",
            flush=True,
        )
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        client.close()


def _direct_scalp_research_loop(symbol: str) -> None:
    from app.services.scalp_research_runner import run_scalp_research_loop

    run_scalp_research_loop(symbol)


def _start_direct_iex_research_loop() -> None:
    if settings.environment != "production" or not settings.iex_research_enabled:
        return
    if os.environ.get("PAPER_DIRECT_RESEARCH_LOOP", "false").strip().lower() != "true":
        return
    threading.Thread(
        target=_direct_iex_research_loop,
        name="direct-iex-research-loop",
        daemon=True,
    ).start()
    print("Direct IEX research loop enabled; execution remains disabled", flush=True)


def _start_direct_scalp_research_loops() -> None:
    if settings.environment != "production" or not settings.iex_research_enabled:
        return
    if os.environ.get("PAPER_DIRECT_SCALP_RESEARCH_LOOP", "false").strip().lower() != "true":
        return
    configured = os.environ.get("PAPER_SCALP_RESEARCH_SYMBOLS", "AAPL,MSFT,SPY,QQQ")
    symbols = sorted({value.strip().upper() for value in configured.split(",") if value.strip()})
    for symbol in symbols:
        threading.Thread(
            target=_direct_scalp_research_loop,
            args=(symbol,),
            name=f"direct-scalp-research-{symbol.lower()}",
            daemon=True,
        ).start()
    print(
        "Direct scalp research loops enabled; "
        f"symbols={','.join(symbols)} execution remains disabled",
        flush=True,
    )


@app.get("/api/research/scalp/runs")
def hosted_scalp_runs(limit: int = 20) -> dict:
    """Serve the read-only scalp history without importing the task graph."""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import ScalpResearchRun
    from app.services.scalp_research import project_scalp_research

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ScalpResearchRun)
            .order_by(ScalpResearchRun.created_at.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
        return {"items": [project_scalp_research(row) for row in rows], "paper_only": True}
    finally:
        db.close()


# The lightweight read-only route is registered before the provider-heavy
# graph. The remaining scalp write/detail routes stay behind the broad loader.
_scalp_routes_ready = True
print("FastAPI scalp research routes ready", flush=True)

print("FastAPI module import complete", flush=True)


@app.on_event("startup")
def on_startup() -> None:
    print("FastAPI startup validation started", flush=True)
    production_blockers = settings.validate_production_configuration()
    if production_blockers:
        # Do not include values from the configuration in the error. The process
        # must not start in an ambiguous production security state.
        raise RuntimeError("Production security configuration is incomplete: " + "; ".join(production_blockers))
    if settings.environment != "production":
        from app.db.schema import assert_schema_current
        from app.db.session import SessionLocal, engine
        from app.models import models, stock_paper  # noqa: F401
        from app.services.seed import seed_defaults

        # Local and test environments retain strict schema verification. The
        # hosted supervisor runs and supervises migrations before this process;
        # production API boot must not block on a second database round trip.
        assert_schema_current(engine)
    if settings.environment == "production":
        # A managed database can briefly stall while the hosting platform is
        # waking it. Do not hold Uvicorn health readiness on optional default
        # rows; the bounded connection timeout and this supervised background
        # seed let the API become observable and fail closed if seeding fails.
        def seed_in_background() -> None:
            from app.db.session import SessionLocal
            from app.services.seed import seed_defaults

            db = SessionLocal()
            try:
                seed_defaults(db)
            except Exception as exc:  # pragma: no cover - hosted-only recovery path
                print(f"Production default seeding deferred: {exc.__class__.__name__}", flush=True)
            finally:
                db.close()

        threading.Thread(target=seed_in_background, name="seed-defaults", daemon=True).start()
        # Let the gateway and lightweight health endpoint become observable
        # while database/model imports and optional provider routers load.
        threading.Thread(
            target=load_application_routes,
            name="load-api-routes",
            daemon=True,
        ).start()
        _start_direct_iex_research_loop()
        _start_direct_scalp_research_loops()
        print("FastAPI startup validation complete", flush=True)
        return

    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()
    load_application_routes()
    print("FastAPI startup validation complete", flush=True)
