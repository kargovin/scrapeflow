import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.settings import settings
from app.routers import health

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ScrapeFlow API", env=settings.app_env)
    yield
    logger.info("Shutting down ScrapeFlow API")


app = FastAPI(
    title="ScrapeFlow API",
    description="Multi-tenant web scraping platform",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# TODO(k8s): replace allow_origins=["*"] with origins loaded from ALLOWED_ORIGINS env var
# (e.g. ALLOWED_ORIGINS="https://scrapeflow.govindappa.com") and remove wildcard methods/headers.
# allow_credentials=True + wildcard origin is invalid in browsers — must be explicit origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
