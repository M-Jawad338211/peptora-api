# Peptora API

Python FastAPI backend for Peptora — peptide research intelligence platform.

## Stack
- Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL (asyncpg)
- JWT auth (httpOnly cookies), NOWPayments crypto billing, Anthropic Claude AI
- Deployed on Railway → https://api.peptora.io
  (also https://peptora-api-production.up.railway.app; `api.peptora.app`
  is NOT this service — that host returns a Vercel 404)

## Local dev
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values; .env.local overrides it and is git-ignored
alembic upgrade head
uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health
```

## Key rules
- All inputs validated with Pydantic
- Never raw SQL — always SQLAlchemy ORM
- Rate limit every public endpoint via slowapi
- Never expose stack traces to clients
- Access is a PREPAID WINDOW, not a subscription status. Crypto cannot
  auto-charge, so `users.paid_until` / `trial_ends_at` are the source of
  truth and `has_access()` is the only correct gate — never `user.plan`,
  which a nightly sweep refreshes and which therefore lags by up to a day.
- Paid tools (protocols, tracker, calculator history, AI) use
  `get_current_subscriber` and return 402. Encyclopedia and stacks stay free.
- Calculator limits: 5 anonymous uses → signup wall; signed-in means 14-day
  trial then paywall. There is no standing free tier.
- Payments credit ONLY on IPN `payment_status == "finished"`, guarded by
  `crypto_payments.credited_at` — callbacks are retried and a duplicate must
  not buy a second month.
- JWT in httpOnly cookies only, never localStorage

## Structure
- `app/routers/` — auth, calculator, subscriptions (crypto), ai, admin
- `app/utils/nowpayments.py` — invoice creation + IPN signature verification
- `app/middleware/` — JWT auth dependency, rate limiter
- `app/utils/` — security (JWT/bcrypt), email (Resend), fingerprinting
- `app/models.py` — all DB tables
- `app/schemas.py` — all Pydantic request/response models

## Deploy
```bash
railway login && railway link && railway up
```
The Docker CMD runs `alembic upgrade head` before uvicorn. Startup's
`create_tables()` only CREATEs missing tables — it never ALTERs existing ones,
so without that step a schema change boots a server that 500s on every query.
