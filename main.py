import sys
import asyncio
from fastapi import FastAPI
from fastapi import Request
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from utils.logger import get_logger
from utils.config import settings
from routers import (
    vector_stores_router,
    files_router,
    search_router,
    sharepoint_router,
    sources_router,
    directories_router,
    schedules_router,
)

from utils.worker import watch_process, stop_event, worker_procs, terminate_worker_group

logger = get_logger(__name__)


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 60)
    print(f"✨ {settings.APP_NAME} {settings.APP_VERSION}")
    print("=" * 60 + "\n")

    stop_event.clear()

    tasks = [
        asyncio.create_task(
            watch_process("vector-worker", [sys.executable, "-m", "workers.vector"])
        ),
        asyncio.create_task(
            watch_process("sharepoint-worker", [sys.executable, "-m", "workers.sharepoint"])
        ),
        asyncio.create_task(
            watch_process("scheduler", [sys.executable, "-m", "workers.scheduler"])
        ),
    ]

    try:
        yield

    finally:
        stop_event.set()

        # termina gruppi (async, non blocca l'event loop)
        for p in worker_procs.values():
            await terminate_worker_group(p)

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Sophia Vector API",
    description="OpenAI Compatible Vector Store with Qdrant backend",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    license_info={
        "name": "Sophia AI Cloud",
        "url": "https://www.sophia-cloud.com",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(sharepoint_router)
app.include_router(vector_stores_router)
app.include_router(files_router)
app.include_router(search_router)
app.include_router(sources_router)
app.include_router(directories_router)
app.include_router(schedules_router)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    print(f"❌ {request.method} {request.url.path} -> {exc.status_code}: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/", include_in_schema=False)
async def root():
    return {"status": f"{settings.APP_NAME} {settings.APP_VERSION} is running 🚀"}


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
