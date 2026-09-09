# Peptora API

Python FastAPI backend for Peptora — peptide research intelligence platform.

## Stack
- Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL (asyncpg)
- JWT auth (httpOnly cookies), manual billing, Anthropic Claude AI
- Railway Bucket (private, S3-compatible) for payment receipts
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
- Rate limit every endpoint via slowapi, admin routes included
- Never expose stack traces to clients
- `has_access()` in `app/middleware/auth.py` is the ONLY correct gate. Never
  `user.plan`, which a nightly sweep refreshes and which therefore lags by up
  to a day. Its check order is load-bearing: `access_revoked_at` is tested
  FIRST, because a refunded user may still be inside their original trial and
  checking it last would make revocation silently do nothing.
- THE WHOLE APP IS PAID. `get_current_subscriber` (402) gates every product
  router — peptides and stacks included. Only auth, consent and `/billing`
  are reachable without a licence. There is no free tier and no anonymous
  calculator allowance.
- Access comes from three places, in descending permanence:
  `lifetime_access_at` (the one-time purchase), `paid_until` (the dormant
  crypto rail), `trial_ends_at` (14 days, granted once per device).
- Trials are bound to `users.signup_fingerprint` via the insert-once
  `trial_grants` table. This CANNOT use `TrialCounter` — its `user_id` is
  UNIQUE and gets reassigned when a second account registers on the same
  fingerprint, so it can never testify that a device already had a trial.
  Fallback fingerprints fail open.
- Approval is one transaction: claim status, `users.lifetime_access_at` and
  the audit row together. It is idempotent — re-approving never moves the
  grant date, so a double-clicked button is harmless.
- Only ONE open claim per user, enforced by a partial unique index. Let the
  constraint arbitrate; the SELECT before it races.
- Receipts are financial PII. Type is sniffed from magic bytes, never the
  header; SVG is refused outright; images are re-encoded to strip EXIF and
  kill polyglots; reads are 5-minute presigned URLs, never public.
- Price and bank details live in the `app_settings` row, not config.py, so
  changing them never needs a redeploy.
- JWT in httpOnly cookies only, never localStorage

## Structure
- `app/routers/billing.py` — instructions, claims, receipt upload (customer)
- `app/routers/admin.py` — queue, approve/reject, grant/revoke, settings
- `app/routers/subscriptions.py` — dormant crypto rail, off by default behind
  `app_settings.crypto_payments_enabled`
- `app/utils/storage.py` — receipt validation + S3/local backends
- `app/utils/settings_store.py` — the single `app_settings` row
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
