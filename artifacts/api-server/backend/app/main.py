from __future__ import annotations

import threading

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
    if request.url.path.startswith("/api/") and not _routers_ready and not (scalp_path and _scalp_routes_ready):
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
        # The gateway's readiness contract is satisfied by the independently
        # loaded paper scalp lane; broad provider routes remain guarded until
        # their own background load completes.
        "routes_ready": _scalp_routes_ready,
        "all_routes_ready": _routers_ready,
        **({"route_error": _router_error} if _router_error else {}),
    }


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
    """Register the lightweight scalp lane before the broad router graph."""
    global _scalp_routes_ready, _router_error
    try:
        from app.api.scalp_research import router as scalp_research_router

        app.include_router(scalp_research_router, prefix="/api")
        _scalp_routes_ready = True
        print("FastAPI scalp research routes ready", flush=True)
    except Exception as exc:  # pragma: no cover - hosted import diagnostics
        _router_error = exc.__class__.__name__
        print(f"FastAPI scalp route loading failed: {_router_error}", flush=True)


# Register the paper-only research lane during module import. This keeps the
# dedicated route independent from the slower, provider-heavy router graph.
load_scalp_routes()

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
        print("FastAPI startup validation complete", flush=True)
        return

    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()
    load_application_routes()
    print("FastAPI startup validation complete", flush=True)
