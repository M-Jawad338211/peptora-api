from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    STRIPE_SECRET_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    STRIPE_MONTHLY_PRICE_ID: str
    STRIPE_ANNUAL_PRICE_ID: str

    ANTHROPIC_API_KEY: str
    RESEND_API_KEY: str
    FROM_EMAIL: str = "noreply@peptora.app"

    FRONTEND_URL: str = "https://peptora.app"
    ADMIN_URL: str = "https://admin.peptora.app"
    ENVIRONMENT: str = "production"

    @property
    def allowed_origins(self) -> List[str]:
        origins = [
            "https://peptora.app",
            "https://www.peptora.app",
            "https://admin.peptora.app",
        ]
        if self.ENVIRONMENT == "development":
            origins += ["http://localhost:3000", "http://localhost:3001"]
        return origins

    class Config:
        env_file = ".env"


settings = Settings()
