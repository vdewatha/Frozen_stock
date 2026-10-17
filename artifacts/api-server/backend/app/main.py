from __future__ import annotations

import threading

print("FastAPI module import started", flush=True)


def _import_stage(name: str) -> None:
    print(f"FastAPI import stage complete: {name}", flush=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
_import_stage("fastapi")

from app.api.routes import router
_import_stage("routes")
from app.api.research import router as research_router
_import_stage("research")
from app.api.agent_research import router as agent_research_router
_import_stage("agent_research")
from app.api.scalp_research import router as scalp_research_router
_import_stage("scalp_research")
from app.api.crypto_research import router as crypto_router
_import_stage("crypto_research")
from app.api.shadow_evidence import router as shadow_evidence_router
_import_stage("shadow_evidence")
from app.api.stock_paper import router as stock_paper_router
_import_stage("stock_paper")
from app.core.config import settings
_import_stage("config")
from app.core.security import AuthenticationMiddleware
_import_stage("security")
from app.db.schema import assert_schema_current
_import_stage("schema")
from app.db.session import SessionLocal, engine
_import_stage("session")
from app.models import models, stock_paper  # noqa: F401
_import_stage("models")
from app.services.seed import seed_defaults
_import_stage("seed")

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(AuthenticationMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
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
            db = SessionLocal()
            try:
                seed_defaults(db)
            except Exception as exc:  # pragma: no cover - hosted-only recovery path
                print(f"Production default seeding deferred: {exc.__class__.__name__}", flush=True)
            finally:
                db.close()

        threading.Thread(target=seed_in_background, name="seed-defaults", daemon=True).start()
        print("FastAPI startup validation complete", flush=True)
        return

    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()
    print("FastAPI startup validation complete", flush=True)


app.include_router(router, prefix="/api")
app.include_router(research_router, prefix="/api")
app.include_router(agent_research_router, prefix="/api")
app.include_router(scalp_research_router, prefix="/api")
app.include_router(crypto_router, prefix="/api")
app.include_router(shadow_evidence_router, prefix="/api")
app.include_router(stock_paper_router, prefix="/api")
app.include_router(stock_training_router, prefix="/api")
app.include_router(stock_forward_trial_router, prefix="/api")
app.include_router(stock_learning_cycle_router, prefix="/api")
app.include_router(live_broker_router, prefix="/api")
app.include_router(live_pilot_router, prefix="/api")
