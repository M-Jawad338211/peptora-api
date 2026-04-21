# Peptora API

Python FastAPI backend for Peptora — peptide research intelligence platform.

## Stack
- Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL (asyncpg)
- JWT auth (httpOnly cookies), Stripe subscriptions, Anthropic Claude AI
- Deployed on Railway → https://api.peptora.app

## Local dev
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values
alembic upgrade head
uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health
```

## Key rules
- All inputs validated with Pydantic
- Never raw SQL — always SQLAlchemy ORM
- Rate limit every public endpoint via slowapi
- Never expose stack traces to clients
- Calculator trial limits: 5 anonymous → signup wall, 25 free → paywall, Pro = unlimited
- JWT in httpOnly cookies only, never localStorage

## Structure
- `app/routers/` — auth, calculator, subscriptions, ai, admin
- `app/middleware/` — JWT auth dependency, rate limiter
- `app/utils/` — security (JWT/bcrypt), email (Resend), fingerprinting
- `app/models.py` — all DB tables
- `app/schemas.py` — all Pydantic request/response models

## Deploy
```bash
railway login && railway link && railway up
```
