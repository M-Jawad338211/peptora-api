from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import List, Optional


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    STRIPE_MONTHLY_PRICE_ID: Optional[str] = None
    STRIPE_ANNUAL_PRICE_ID: Optional[str] = None

    ANTHROPIC_API_KEY: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    FROM_EMAIL: str = "noreply@peptora.app"

    @field_validator("FROM_EMAIL", mode="before")
    @classmethod
    def strip_email_quotes(cls, v: str) -> str:
        return v.strip().strip('"').strip("'")

    FRONTEND_URL: str = "https://peptora.app"
    ADMIN_URL: str = "https://admin.peptora.app"
    CORS_ORIGINS: str = "https://peptora.app,https://www.peptora.app,https://admin.peptora.app"
    ENVIRONMENT: str = "production"

    @property
    def allowed_origins(self) -> List[str]:
        origins = [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        if self.ENVIRONMENT == "development":
            origins += ["http://localhost:3000", "http://localhost:3001", "http://localhost:8081"]
        return list(dict.fromkeys(origins))

    class Config:
        env_file = ".env"


settings = Settings()
