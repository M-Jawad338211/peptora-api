from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import List, Optional

# Hosts that mean "this machine" — WEB_URL/ADMIN_URL pointing at any of these
# puts the API into local-dev mode (see Settings.is_development).
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# Origins the local web app / Expo dev server run on.
LOCAL_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:8081",
]


def _is_local_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in LOCAL_HOSTNAMES or host.endswith(".localhost")


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # NOWPayments — crypto is the only payment rail. There is no card
    # processor and no auto-charge: see app/utils/nowpayments.py.
    NOWPAYMENTS_API_KEY: Optional[str] = None
    NOWPAYMENTS_IPN_SECRET: Optional[str] = None
    NOWPAYMENTS_API_URL: str = "https://api.nowpayments.io/v1"

    # This API's own public origin — where NOWPayments sends IPN callbacks.
    # It must NOT be WEB_URL: the web app proxies /api/* through Vercel, and
    # the IPN signature is computed over raw bytes, so an extra hop is a free
    # way to break verification. Callbacks go straight to the API host.
    API_PUBLIC_URL: str = "https://api.peptora.io"

    # Access windows, in days. The trial is granted once, at email
    # verification, and is bound to the signup device fingerprint.
    # PLAN_DAYS[plan] still drives the dormant crypto rail.
    TRIAL_DAYS: int = 14
    # Whether an account created in the iOS app gets that account trial. Off:
    # on the App Store the free trial is the subscription's own introductory
    # offer, which Apple runs and bills. A second, separate trial handed out by
    # this API would be Pro unlocked outside In-App Purchase, which guideline
    # 3.1.1 does not allow. Accounts created on the web are unaffected.
    TRIAL_FOR_IOS_SIGNUPS: bool = False
    PRICE_MONTHLY_USD: float = 5.0
    PRICE_ANNUAL_USD: float = 49.0

    # Fallback for the one-time price, used only to seed app_settings on a
    # fresh database. The live figure is the one in app_settings, editable
    # from the admin panel — bank details and prices change, and neither
    # should need a redeploy.
    PRICE_ONETIME_USD: float = 99.0

    # Railway Bucket holding payment receipts. Absent locally, where
    # app/utils/storage.py falls back to a directory on disk.
    RECEIPTS_BUCKET: Optional[str] = None
    RECEIPTS_ENDPOINT: Optional[str] = None
    RECEIPTS_REGION: Optional[str] = None
    RECEIPTS_ACCESS_KEY_ID: Optional[str] = None
    RECEIPTS_SECRET_ACCESS_KEY: Optional[str] = None

    # Receipts are financial PII. Objects are deleted this long after the
    # claim is resolved; the claim metadata itself is kept indefinitely as
    # the record of why an account has access.
    RECEIPT_RETENTION_DAYS: int = 365

    # Apple In-App Purchase: the iOS app's Peptora Pro subscriptions.
    #
    # There is no shared secret to configure. The App Store signs every
    # transaction and notification, and app/utils/apple_iap.py checks that
    # signature against Apple's root certificate (bundled in app/certs). These
    # settings only say which app and which products this API will accept.
    APPLE_BUNDLE_ID: str = "app.peptora"
    # The app's numeric Apple ID (App Store Connect -> App Information).
    # Apple includes it in Production notifications and the verifier checks it.
    APPLE_APP_ID: Optional[int] = 6772127291
    APPLE_IAP_PRODUCT_IDS: str = "app.peptora.pro.monthly,app.peptora.pro.yearly"
    # Leave this on in production. App Review, TestFlight and sandbox testers
    # all buy in Apple's Sandbox environment from the production build, and
    # they all talk to this API. Turning it off makes every one of those
    # purchases fail verification, which App Review reads as a broken paywall.
    APPLE_IAP_ALLOW_SANDBOX: bool = True
    # Online revocation checks (OCSP) call Apple during verification. Off by
    # default: the signature and certificate chain are still fully verified
    # offline, and a slow or unreachable OCSP responder would otherwise turn
    # into failed purchases.
    APPLE_IAP_ONLINE_CHECKS: bool = False

    # The AI endpoints (app/routers/ai.py) are switched off. No Peptora client
    # has an AI feature, and the privacy policy says that nothing a user
    # enters is sent to an AI provider, so the routes are not even registered
    # unless this is turned on. Turning it on means updating that policy too.
    AI_ENABLED: bool = False
    ANTHROPIC_API_KEY: Optional[str] = None
    CRON_SECRET: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    FROM_EMAIL: str = "noreply@peptora.app"

    @field_validator("FROM_EMAIL", mode="before")
    @classmethod
    def strip_email_quotes(cls, v: str) -> str:
        return v.strip().strip('"').strip("'")

    # Public web app origin. Accepts https:// for deployed environments and
    # http:// for localhost, so a local .env can point this at the dev server.
    WEB_URL: str = "https://peptora.io"
    ADMIN_URL: str = "https://admin.peptora.io"
    CORS_ORIGINS: str = "https://peptora.io,https://www.peptora.io,https://admin.peptora.io"
    ENVIRONMENT: str = "production"

    @field_validator("WEB_URL", "ADMIN_URL", "API_PUBLIC_URL", mode="before")
    @classmethod
    def normalize_origin(cls, v: str) -> str:
        url = v.strip().strip('"').strip("'").rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"must be an absolute http(s) URL with no trailing path, got {v!r}"
            )
        if parsed.scheme == "http" and not _is_local_url(url):
            raise ValueError(
                f"http:// is only allowed for localhost addresses, got {v!r}"
            )
        # Keep origins bare (scheme://host[:port]) so they match browser Origin headers.
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def receipts_configured(self) -> bool:
        """Whether the object store is usable. All five must be present —
        a partial config would fail at the first upload, after the user has
        already filled in the form."""
        return all([
            self.RECEIPTS_BUCKET,
            self.RECEIPTS_ENDPOINT,
            self.RECEIPTS_ACCESS_KEY_ID,
            self.RECEIPTS_SECRET_ACCESS_KEY,
        ])

    @property
    def payments_configured(self) -> bool:
        """Checkout is only offered when both halves of the integration exist.

        The API key alone is not enough — without the IPN secret every callback
        would fail verification, so users could pay and never be credited.
        """
        return bool(self.NOWPAYMENTS_API_KEY and self.NOWPAYMENTS_IPN_SECRET)

    @property
    def apple_product_ids(self) -> frozenset:
        """Product identifiers this API accepts from the App Store."""
        return frozenset(
            p.strip() for p in self.APPLE_IAP_PRODUCT_IDS.split(",") if p.strip()
        )

    @property
    def is_development(self) -> bool:
        """True when explicitly set, or when the web app is running locally."""
        return self.ENVIRONMENT == "development" or _is_local_url(self.WEB_URL)

    @property
    def allowed_origins(self) -> List[str]:
        origins = [origin.strip().rstrip("/") for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        # The web and admin apps are always allowed to call the API.
        origins += [self.WEB_URL, self.ADMIN_URL]
        if self.is_development:
            origins += LOCAL_DEV_ORIGINS
        return list(dict.fromkeys(origins))

    class Config:
        # .env.local is git-ignored and overrides .env, so a local checkout can
        # point WEB_URL etc. at localhost without touching the shared file.
        env_file = (".env", ".env.local")


settings = Settings()
