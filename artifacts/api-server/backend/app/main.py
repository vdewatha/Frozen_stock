from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.research import router as research_router
from app.api.crypto_research import router as crypto_router
from app.api.shadow_evidence import router as shadow_evidence_router
from app.api.stock_paper import router as stock_paper_router
from app.core.config import settings
from app.core.security import AuthenticationMiddleware
from app.db.schema import assert_schema_current
from app.db.session import SessionLocal, engine
from app.models import models, stock_paper  # noqa: F401
from app.services.seed import seed_defaults

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


@app.on_event("startup")
def on_startup() -> None:
    assert_schema_current(engine)
    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()


app.include_router(router, prefix="/api")
app.include_router(research_router, prefix="/api")
app.include_router(crypto_router, prefix="/api")
app.include_router(shadow_evidence_router, prefix="/api")
app.include_router(stock_paper_router, prefix="/api")
app.include_router(stock_training_router, prefix="/api")
