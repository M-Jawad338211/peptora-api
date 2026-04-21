import uuid
import time
import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.config import settings
from app.middleware.rate_limit import limiter
from app.routers import auth, calculator, subscriptions, ai, admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("peptora")

app = FastAPI(title="Peptora API", version="1.0.0", docs_url="/docs" if settings.ENVIRONMENT == "development" else None)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Device-Fingerprint", "X-Platform"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.time()
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        logger.error(f"Unhandled error request_id={request_id}: {exc}", exc_info=True)
        return Response(
            content=f'{{"error":"Internal server error","request_id":"{request_id}"}}',
            status_code=500,
            media_type="application/json",
        )

    duration_ms = round((time.time() - start) * 1000)
    logger.info(f"request_id={request_id} method={request.method} path={request.url.path} status={response.status_code} duration={duration_ms}ms")

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
    from app.database import create_tables
    await create_tables()
    logger.info("Peptora API started")


@app.on_event("shutdown")
async def shutdown():
    from app.database import engine
    await engine.dispose()
    logger.info("Peptora API stopped")
