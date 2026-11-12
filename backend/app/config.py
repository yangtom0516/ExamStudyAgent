"""Settings loaded from environment variables, or from the .env file at the repo root.

Inside Docker there is no .env file; docker-compose passes the same names as environment variables.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds keys only docker-compose uses, e.g. POSTGRES_PASSWORD
    )

    # Data stores (required)
    database_url: str
    redis_url: str
    qdrant_url: str
    qdrant_collection: str = ""

    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: SecretStr
    s3_bucket: str
    max_upload_mb: int = 50

    # Auth
    jwt_secret: SecretStr  # required
    jwt_expire_minutes: int = 60 * 24 * 7  # login lasts 7 days
    cookie_secure: bool = False  # True in the cloud deploy: send the login cookie over HTTPS only

    # Gemini (needed from build step 3 on)
    google_api_key: SecretStr = SecretStr("")
    coach_model: str = ""
    exam_model: str = ""
    grader_model: str = ""
    ocr_model: str = ""
    embed_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
