FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Migrations run before the server binds. Railway deploys this image
# directly, and app startup only creates missing TABLES — it never adds
# columns to existing ones, so a schema change without this line boots a
# server that 500s on every query touching the new columns.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
