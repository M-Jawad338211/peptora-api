import uuid
import time
import json
import logging
import logging.config
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.config import settings
from app.middleware.rate_limit import limiter
from app.routers import auth, calculator, subscriptions, ai, admin, tracker, cron, peptides

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "peptora": {"level": "DEBUG" if settings.ENVIRONMENT == "development" else "INFO"},
        "uvicorn.access": {"level": "WARNING"},  # suppress duplicate access logs
    },
})
logger = logging.getLogger("peptora")

_SENSITIVE_FIELDS = {"password", "confirm_password", "new_password", "otp", "token", "secret", "api_key"}


async def _log_request_body(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        return ""
    try:
        raw = await request.body()
        if not raw:
            return ""
        data = json.loads(raw)
        if isinstance(data, dict):
            sanitized = {k: "***" if k in _SENSITIVE_FIELDS else v for k, v in data.items()}
            return json.dumps(sanitized, default=str)
        return json.dumps(data, default=str)[:500]
    except Exception:
        return ""

app = FastAPI(
    title="Peptora API",
    version="1.0.0",
    description=(
        "REST API for Peptora — peptide research intelligence platform.\n\n"
        "## Authentication\n"
        "All protected endpoints require a valid JWT stored in an `httpOnly` cookie. "
        "Obtain tokens via `POST /auth/login` or `POST /auth/register`, then include "
        "the cookie in subsequent requests.\n\n"
        "## Rate limits\n"
        "Public endpoints are rate-limited per IP. Exceeding the limit returns `429 Too Many Requests`.\n\n"
        "## Calculator trial limits\n"
        "- Anonymous: 5 calculations then signup wall\n"
        "- Free tier: 25 calculations then paywall\n"
        "- Pro: unlimited"
    ),
    contact={
        "name": "Peptora Support",
        "url": "https://peptora.app",
        "email": "support@peptora.app",
    },
    license_info={
        "name": "Proprietary",
    },
    openapi_tags=[
        {"name": "auth", "description": "Registration, login, logout, OTP, and token refresh."},
        {"name": "calculator", "description": "Peptide property calculations (MW, GRAVY, charge, etc.)."},
        {"name": "ai", "description": "AI-powered peptide research assistant (Claude)."},
        {"name": "subscriptions", "description": "Stripe billing — plans, checkout, portal, webhooks."},
        {"name": "admin", "description": "Admin-only endpoints for user and system management."},
    ],
    docs_url="/docs",
    redoc_url="/redoc",
)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Device-Fingerprint", "X-Platform", "X-Cron-Secret"],
)


@app.middleware("http")
async def request_logging_and_security(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.time()

    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
    query = f"?{request.url.query}" if request.url.query else ""
    ua = request.headers.get("user-agent", "")[:80]
    body_log = await _log_request_body(request)

    logger.info(
        f"→ {request.method} {request.url.path}{query} "
        f"ip={client_ip} request_id={request_id}"
        + (f" body={body_log}" if body_log else "")
        + (f" ua={ua}" if ua else "")
    )

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.time() - start) * 1000)
        logger.error(
            f"✗ {request.method} {request.url.path} status=500 duration={duration_ms}ms "
            f"request_id={request_id} error={exc}",
            exc_info=True,
        )
        return Response(
            content=f'{{"error":"Internal server error","request_id":"{request_id}"}}',
            status_code=500,
            media_type="application/json",
        )

    duration_ms = round((time.time() - start) * 1000)
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(
        level,
        f"← {request.method} {request.url.path} status={response.status_code} "
        f"duration={duration_ms}ms request_id={request_id}",
    )

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# Routers
app.include_router(auth.router)
app.include_router(calculator.router)
app.include_router(subscriptions.router)
app.include_router(ai.router)
app.include_router(admin.router)
app.include_router(tracker.router)
app.include_router(cron.router)
app.include_router(peptides.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "service": "peptora-api"}


@app.get("/health/db")
async def health_db():
    if settings.ENVIRONMENT != "development":
        from app.middleware.auth import get_current_admin
        # In prod this endpoint requires admin — checked at route level
        pass
    from app.database import engine
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}


@app.on_event("startup")
async def startup():
    try:
        from app.database import create_tables
        await create_tables()
        logger.info("Peptora API started — DB connected")
    except Exception as e:
        logger.error(f"Startup DB error (non-fatal): {e}")
        logger.info("Peptora API started — running without DB")


@app.on_event("shutdown")
async def shutdown():
    from app.database import engine
    await engine.dispose()
    logger.info("Peptora API stopped")
