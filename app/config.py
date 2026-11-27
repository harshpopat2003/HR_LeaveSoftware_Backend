"""Settings, read from environment variables or a .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-secret-change-me-in-production-please-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://hr_app:hr_dev_password@localhost:5432/hr_leave"
    test_database_url: str = "postgresql+psycopg://hr_app:hr_dev_password@localhost:5432/hr_test"
    jwt_secret: str = DEV_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cors_origins: str = "http://localhost:5173"
    cookie_secure: bool = False
    timezone: str = "Asia/Dubai"
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    def check_production(self) -> None:
        """With HTTPS cookies on (production), refuse to start with the development secret."""
        if self.cookie_secure and (self.jwt_secret == DEV_SECRET or len(self.jwt_secret) < 32):
            raise RuntimeError("Set JWT_SECRET to a long random value (32+ characters) before running in production.")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
