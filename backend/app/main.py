from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.routers import accounting, auth, demo, exports, invoices
from app.schemas import HealthResponse, ReadyResponse
from app.settings import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.check_production_safety()
    if settings.run_migrations_on_startup:
        from app.migrate import upgrade_database

        upgrade_database(db.engine)
    yield


app = FastAPI(
    title="InvoiceIQ API",
    description=(
        "Invoice intake, extraction, human review, German VAT compliance checks, India–Germany cross-border "
        "indicators, accounting suggestions and a DATEV-ready export prototype. Formal checks and indicators are "
        "not legal or tax advice."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Organization-ID"],
    expose_headers=["Content-Disposition"],
)


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="InvoiceIQ API", version=app.version)


@app.get("/api/ready", response_model=ReadyResponse, tags=["system"])
def readiness() -> ReadyResponse:
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    return ReadyResponse(status="ready", database="connected")


app.include_router(auth.router)
app.include_router(invoices.router)
app.include_router(accounting.router)
app.include_router(exports.router)
app.include_router(exports.spec_router)
app.include_router(demo.router)
