"""Settings for the Streamers Hub API.

The `.env` file is the same one the school systems use (AWS SES and S3 credentials, the JWT secret
and so on). Anything that must not be shared with a school lives under an `SH_` name, and
`.env.streamershub` (if present) overrides `.env`, so local or production values for Streamers Hub
can be set without editing the shared file:

- Database: `SH_DATABASE_URL`, or else the school's Postgres server with the database name swapped
  for `streamers_hub`, so the label's tables never land in a school database.
- Files: the shared bucket, under the `SH_S3_PREFIX` key prefix.
- Payments: only `SH_PAYSTACK_SECRET_KEY`. The school's own Paystack keys are never used.
"""

import logging
from typing import ClassVar, List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy.engine.url import make_url

# Variables already set in the process (a deploy's own settings, the test harness) win; then
# `.env.streamershub`; then the shared `.env`.
load_dotenv(".env.streamershub", override=False)
load_dotenv(".env", override=False)
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """All settings of the application."""

    ENVIRONMENT: str = "development"

    # Database
    SH_DATABASE_URL: Optional[str] = None
    SH_DATABASE_NAME: str = "streamers_hub"
    APOSTGRES_DATABASE_URL: Optional[str] = None
    APOSTGRES_PRODUCTION_DATABASE_URL: Optional[str] = None
    SQL_ECHO: bool = False

    # Sign-in
    SECRET_KEY: str = Field(default="dev-only-secret-change-me")
    ALGORITHM: str = "HS256"
    SH_SESSION_HOURS: int = 12
    SH_VAULT_SESSION_MINUTES: int = 30
    SH_COOKIE_DOMAIN: Optional[str] = None  # e.g. .streamershubmedia.com in production

    # AWS S3 (files) and SES (email): the same accounts as the schools
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_S3_BUCKET: Optional[str] = None
    AWS_REGION: Optional[str] = None
    SH_S3_PREFIX: str = "streamers-hub"
    AWS_SES_ACCESS_KEY_ID: Optional[str] = None
    AWS_SES_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_SES_REGION: Optional[str] = None
    FROM_EMAIL: Optional[str] = None
    SH_FROM_EMAIL: Optional[str] = None  # a verified SES identity for the label, e.g. no-reply@streamershubmedia.com
    SH_EMAIL_MODE: str = "ses"  # "ses" sends; "log" keeps mail in memory (tests and offline work)
    SH_CONTACT_EMAIL: str = "contact@streamershubmedia.com"

    # Payments (Paystack takes mobile money and cards in Ghana)
    SH_PAYSTACK_SECRET_KEY: str = ""
    SH_PAYSTACK_PUBLIC_KEY: str = ""
    SH_PAYMENTS_TEST_MODE: Optional[bool] = None  # defaults to on outside production when no key is set

    # YouTube Data API v3 key, used only to read public view counts of YouTube Shorts clips. Without it, clips are
    # checked by hand as before.
    SH_YOUTUBE_API_KEY: str = ""

    # Where the website runs, for links in emails and payment redirects
    SH_FRONTEND_URL: str = "http://localhost:3000"
    SH_EXTRA_CORS_ORIGINS: str = ""

    # Personal data at rest (phone, mobile money numbers). Derived from SECRET_KEY when not set.
    SH_DATA_KEY: Optional[str] = None

    SH_RUN_SCHEDULER: bool = True
    SH_BCRYPT_ROUNDS: int = 12
    SH_PROXY_HOPS: int = 0  # 1 behind one load balancer; 0 when the API is reached directly

    @field_validator("SH_DATABASE_URL", "APOSTGRES_DATABASE_URL", "APOSTGRES_PRODUCTION_DATABASE_URL", "SH_COOKIE_DOMAIN",
                     "SH_FROM_EMAIL", "SH_DATA_KEY", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        """`SH_DATABASE_URL=None` or an empty value in a .env file means "not set", not a URL called None."""
        if isinstance(value, str) and value.strip().strip("'\"").lower() in ("", "none", "null"):
            return None
        return value

    DB_MODELS: ClassVar[List[str]] = [
        "app.models.people",
        "app.models.talent",
        "app.models.partnerships",
        "app.models.store",
        "app.models.clips",
        "app.models.applications",
        "app.models.money",
        "app.models.investors",
        "app.models.tech",
        "app.models.agreements",
        "app.models.posts",
        "app.models.contact",
        "app.models.suggestions",
    ]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def database_url(self) -> str:
        if self.SH_DATABASE_URL:
            url = self.SH_DATABASE_URL
        else:
            shared = self.APOSTGRES_PRODUCTION_DATABASE_URL if self.is_production else self.APOSTGRES_DATABASE_URL
            shared = shared or self.APOSTGRES_DATABASE_URL or self.APOSTGRES_PRODUCTION_DATABASE_URL
            if not shared:
                raise RuntimeError("Set SH_DATABASE_URL in .env.streamershub")
            url = make_url(_async_scheme(shared)).set(database=self.SH_DATABASE_NAME).render_as_string(hide_password=False)
        return _async_scheme(url)

    @property
    def from_email(self) -> str:
        return self.SH_FROM_EMAIL or self.FROM_EMAIL or "no-reply@streamershubmedia.com"

    @property
    def payments_test_mode(self) -> bool:
        if self.SH_PAYMENTS_TEST_MODE is not None:
            return self.SH_PAYMENTS_TEST_MODE and not self.is_production
        return not self.SH_PAYSTACK_SECRET_KEY and not self.is_production

    @property
    def cors_origins(self) -> list[str]:
        origins = [self.SH_FRONTEND_URL, "http://localhost:3000", "http://127.0.0.1:3000"]
        if self.is_production:
            origins += ["https://streamershubmedia.com", "https://www.streamershubmedia.com"]
        origins += [o.strip() for o in self.SH_EXTRA_CORS_ORIGINS.split(",") if o.strip()]
        return list(dict.fromkeys(origins))

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


def _async_scheme(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


settings = Settings()
