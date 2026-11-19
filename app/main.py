"""Streamers Hub API: the website, Hub OS, the Investor Vault, Designer Studio and Clipper Desk."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import Settings, settings
from app.core.database import session_manager
from app.services.S3Service import StorageUnavailable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def refuse_unsafe_production() -> None:
    """Production doesn't start on development defaults. Only when ENVIRONMENT=production: tests and local dev
    are never checked."""
    if not settings.is_production:
        return
    from app.services import S3Service

    problems = []
    if not settings.SECRET_KEY or settings.SECRET_KEY == Settings.model_fields["SECRET_KEY"].default:
        problems.append("SECRET_KEY is the development default: set a long random value.")
    problems += S3Service.production_problems()
    if problems:
        raise RuntimeError("Refusing to start in production. " + " ".join(problems))
    if not settings.SH_DATA_KEY:
        logger.warning("SH_DATA_KEY is not set: personal data is encrypted with a key derived from SECRET_KEY, "
                       "so changing SECRET_KEY would make phone and account numbers unreadable.")


async def _website_content() -> None:
    """Make sure the public site has its streamer, roster seats and label merch (see services/Bootstrap.py)."""
    from app.services import Bootstrap

    try:
        async with session_manager.session_factory() as db:
            await Bootstrap.run(db)
    except Exception:  # never stop the API from starting over this
        logger.exception("Could not add the website's starting content")


@asynccontextmanager
async def lifespan(app: FastAPI):
    refuse_unsafe_production()
    await session_manager.init()
    await _website_content()
    task = None
    if settings.SH_RUN_SCHEDULER:
        from app.services.Scheduler import run_forever

        task = asyncio.create_task(run_forever())
    try:
        yield
    finally:
        if task:
            task.cancel()
        await session_manager.close()


def create_app(lifespan_on: bool = True) -> FastAPI:
    app = FastAPI(title="Streamers Hub API", version="1.0.0", lifespan=lifespan if lifespan_on else None,
                  docs_url=None if settings.is_production else "/docs", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith(("/api/v1/auth", "/api/v1/investor", "/api/v1/vault")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        missing = []
        for err in exc.errors():
            loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
            if loc:
                missing.append(loc[-1])
        return JSONResponse(status_code=422, content={"detail": {"message": "Check the highlighted fields.", "missing": missing}})

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable):
        # Answered here, inside CORS, so the website can read it (S3Service logs the full error)
        return JSONResponse(status_code=503, content={"detail": f"We couldn't save the file just now (storage error: {exc.code}). Try again in a minute."})

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = create_app()
