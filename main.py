import sys
import asyncio
from fastapi import FastAPI
from fastapi import Request, Depends
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

from utils.logger import get_logger
from utils.config import settings
from utils.banner import render_banner
from routers import (
    vector_stores_router,
    files_router,
    search_router,
    sharepoint_router,
    sources_router,
    directories_router,
    schedules_router,
    nlp_router,
)

from utils.worker import watch_process, stop_event, worker_procs, terminate_worker_group
from utils.auth import require_api_key

logger = get_logger(__name__)


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    render_banner()

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
    # Docs servite via Scalar (vedi /docs), Swagger/ReDoc di default disattivati.
    docs_url=None,
    redoc_url=None,
)

# CORS: allowlist esplicita via SOPHIA_VECTOR_CORS_ORIGINS (csv). Wildcard "*" +
# credentials è una combo invalida/insicura (i browser la rifiutano, Starlette la
# rifletterebbe): se nessuna origin è configurata ricadiamo su "*" SENZA credentials.
# Il backend non usa cookie cross-origin — l'identità utente passa da x-user-id
# iniettato server-side dal proxy Next, non da credenziali CORS.
_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers — protetti dall'API key opzionale (no-op se SOPHIA_VECTOR_API_KEY è vuota).
# /, /health, /docs e /openapi.json restano liberi (definiti direttamente su app).
_v1 = [Depends(require_api_key)]
app.include_router(sharepoint_router, dependencies=_v1)
app.include_router(vector_stores_router, dependencies=_v1)
app.include_router(files_router, dependencies=_v1)
app.include_router(search_router, dependencies=_v1)
app.include_router(sources_router, dependencies=_v1)
app.include_router(directories_router, dependencies=_v1)
app.include_router(schedules_router, dependencies=_v1)
# Endpoint NLP (/v1/nlp/*): opt-in. Il backend è "owner" dei modelli (lazy, in-process);
# worker e client esterni li usano via HTTP — una sola copia. Spegnibile via env.
if settings.NLP_ENABLED:
    app.include_router(nlp_router, dependencies=_v1)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    print(f"❌ {request.method} {request.url.path} -> {exc.status_code}: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Rete di sicurezza per gli endpoint senza try/except: qualunque eccezione non
    gestita diventa un 500 PULITO (niente str(exc) al client → nessun leak di
    dettagli interni) con il traceback completo nei log server-side."""
    logger.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/", include_in_schema=False)
async def root():
    return {"status": f"{settings.APP_NAME} {settings.APP_VERSION} is running 🚀"}


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    """API reference servita da Scalar (più leggibile di Swagger)."""
    return HTMLResponse(f"""<!doctype html>
<html>
<head>
    <title>Sophia Vector API — Reference</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ margin: 0; }}
        :root {{
            --scalar-color-accent: #6366f1;  /* indigo, brand Sophia Vector */
            --scalar-radius: 6px;
            --scalar-radius-lg: 8px;
        }}
    </style>
</head>
<body>
    <div id="app"></div>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
    <script>
        Scalar.createApiReference("#app", {{
            "url": "/openapi.json",
            "_integration": "fastapi",
            "darkMode": true
        }})
    </script>
</body>
</html>""")
