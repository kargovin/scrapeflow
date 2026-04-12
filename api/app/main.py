import asyncio
from contextlib import asynccontextmanager

import httpx
import structlog
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core import minio, nats
from app.core.db import AsyncSessionLocal
from app.core.redis import close_pool, create_pool
from app.core.result_consumer import start_result_consumer
from app.core.scheduler import scheduler_loop
from app.core.webhook_loop import webhook_delivery_loop
from app.middleware.correlation import CorrelationIdMiddleware
from app.routers import health, jobs, users
from app.settings import settings

logger = structlog.get_logger()


def _run_migrations_online():
    """Run migrations with a live async DB connection."""
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ScrapeFlow API", env=settings.app_env)

    # TODO: uncomment when pusing
    # # Alembic migrations — run in separate thread to avoid blocking the event loop, since Alembic doesn't support async DB connections.
    # try:
    #     await asyncio.get_event_loop().run_in_executor(None, _run_migrations_online)
    #     logger.info("Database migrations complete")
    # except Exception:
    #     logger.exception("Database migration failed")
    #     raise

    # Redis
    app.state.redis_pool = create_pool()
    logger.info("Redis pool created")

    # MinIO
    app.state.minio = await minio.create_client()
    logger.info("MinIO client ready", bucket=settings.minio_bucket)

    # NATS
    app.state.nats_client, app.state.nats_js = await nats.connect()
    logger.info("NATS connected", url=settings.nats_url)

    # Result consumer — background task that processes worker results from NATS (ADR-001)
    result_consumer_task = await start_result_consumer(app.state.nats_js, app.state.minio)
    logger.info("NATS result consumer started")

    # Shared HTTP client for webhook delivery (reused across all deliveries)
    http_client = httpx.AsyncClient()

    # Fernet instance — same key used for LLM key encryption (validated at settings load)
    fernet = Fernet(settings.llm_key_encryption_key.encode())

    # Scheduler — dispatches due cron jobs every 60s
    scheduler_task = asyncio.create_task(scheduler_loop(AsyncSessionLocal, app.state.nats_js))
    logger.info("Scheduler loop started")

    # Webhook delivery loop — retries pending deliveries every 15s
    webhook_task = asyncio.create_task(
        webhook_delivery_loop(AsyncSessionLocal, http_client, fernet)
    )
    logger.info("Webhook delivery loop started")

    yield

    # Shutdown — cancel all background tasks together
    result_consumer_task.cancel()
    scheduler_task.cancel()
    webhook_task.cancel()
    await asyncio.gather(result_consumer_task, scheduler_task, webhook_task, return_exceptions=True)
    logger.info("Background tasks stopped")

    await http_client.aclose()
    logger.info("HTTP client closed")

    await nats.disconnect(app.state.nats_client)
    logger.info("NATS disconnected")

    await minio.close_client(app.state.minio)
    logger.info("MinIO client closed")

    await close_pool(app.state.redis_pool)
    logger.info("Redis pool closed")

    logger.info("ScrapeFlow API shutdown complete")


app = FastAPI(
    title="ScrapeFlow API",
    description="Multi-tenant web scraping platform",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=settings.allowed_origins
    != ["*"],  # only allow credentials if specific origins are set
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(users.router)
app.include_router(jobs.router)
