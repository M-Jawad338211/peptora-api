# Peptora API

FastAPI backend for [Peptora](https://peptora.app) — the peptide research intelligence platform.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /auth/register | — | Create account |
| POST | /auth/login | — | Login |
| POST | /auth/logout | Cookie | Logout |
| GET | /auth/me | Cookie | Current user + plan |
| POST | /calculator/check-trial | Optional | Gate check before calculation |
| POST | /calculator/record-use | Optional | Record a calculation |
| GET | /calculator/history | Pro | Last 100 calculations |
| POST | /subscriptions/create-checkout | Free | Create Stripe checkout |
| POST | /subscriptions/webhook | Stripe | Stripe webhook handler |
| GET | /subscriptions/portal | Pro | Billing portal |
| POST | /ai/assistant | Pro | AI research chat |
| POST | /ai/stack-check | Pro | Peptide stack analysis |
| GET | /admin/stats | Admin | Platform overview |
| GET | /admin/users | Admin | User management |

## Local Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in all values

# Optional: point the API at a locally-running web app.
# .env.local is git-ignored and overrides .env.
printf 'WEB_URL=http://localhost:3000\nADMIN_URL=http://localhost:3001\nENVIRONMENT=development\n' > .env.local

# Create DB tables
alembic upgrade head

# Run
uvicorn app.main:app --reload --port 8000

# Verify
curl http://localhost:8000/health
```

## Deploy to Railway

```bash
npm install -g @railway/cli
railway login
railway init     # name: peptora-api
# In Railway dashboard: add PostgreSQL plugin, copy DATABASE_URL to Variables
# Add all other .env.example variables to Railway Variables
railway up
```

## Stripe webhook (local testing)

```bash
stripe listen --forward-to localhost:8000/subscriptions/webhook
```
