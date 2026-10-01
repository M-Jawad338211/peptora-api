# Peptora API

Python FastAPI backend for Peptora — peptide research intelligence platform.

## Stack
- Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL (asyncpg)
- JWT auth (httpOnly cookies), manual billing, App Store subscriptions
- No AI. `app/routers/ai.py` is legacy and is not registered unless
  `AI_ENABLED=true`; no client calls it and the privacy policy says no user
  data goes to an AI provider.
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
- THE LIBRARY IS FREE, THE ACCOUNT FEATURES ARE PAID. `/peptides` and
  `/stacks` are public reference content with no auth at all. App Review
  rejected the app under guideline 5.1.1(v) when they sat behind a login, so
  do not put them back behind one. `get_current_subscriber` (402) gates what a
  user saves: protocols, the log, calculation history. Auth, consent,
  `/billing` and `/iap` are reachable without access.
- Access comes from four places, in descending permanence:
  `lifetime_access_at` (the one-time purchase on the web), `apple_sub_until`
  (the App Store subscription bought in the iOS app), `paid_until` (the
  dormant crypto rail), `trial_ends_at` (14 days, granted once per device).
- The account trial is NOT granted to an account created in the iOS app
  (`X-Platform: ios` at `/auth/verify-email`). On the App Store the free
  trial is the subscription's introductory offer, which Apple runs; a second
  trial from this API would be Pro unlocked outside In-App Purchase
  (guideline 3.1.1). `TRIAL_FOR_IOS_SIGNUPS=true` turns it back on.
- App Store purchases are never trusted from the client. The app posts the
  signed transaction (a JWS) and `app/utils/apple_iap.py` verifies Apple's
  signature against the root certificate pinned in `app/certs`. Only
  Production and Sandbox are accepted: Apple's library skips signature checks
  for its Xcode and LocalTesting environments, so a client must never be able
  to select one. Sandbox stays enabled in production because App Review and
  TestFlight buy in Sandbox.
- A subscription follows the Apple ID: whichever account presents a valid
  transaction owns it (`apple_subscriptions.user_id` moves). Rows only move
  forward, so replayed or out-of-order transactions cannot undo a refund.
- Account deletion (`POST /auth/delete-account`) is real deletion, required by
  App Store guideline 5.1.1(v). If you add a table with a `user_id`, add it to
  that endpoint, or deleting an account will start failing on the foreign key.
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
- `app/routers/iap.py` — App Store subscriptions: `/iap/apple/verify` (from
  the app) and `/iap/apple/notifications` (from Apple)
- `app/utils/apple_iap.py` — JWS verification for transactions and App Store
  Server Notifications V2
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
