"""Settings loaded from environment variables, or from the .env file at the repo root.

Inside Docker there is no .env file; docker-compose passes the same names as environment variables.
"""

import re
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
        env_ignore_empty=True,  # `KEY=` with no value means "use the default", not ""
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

    # Gemini. The API key is needed by the ingestion worker (embeddings) and, later, the agents.
    google_api_key: SecretStr = SecretStr("")
    coach_model: str = ""
    exam_model: str = ""
    grader_model: str = ""
    ocr_model: str = "gemini-3.5-flash-lite"  # cheap, fast, reads PDFs and images
    max_ocr_pages: int = 100  # per file; caps the cost of one huge scanned upload
    topic_model: str = "gemini-3.5-flash-lite"  # topic extraction (one call per document)
    embed_model: str = "gemini-embedding-2"
    embed_dimensions: int = 768  # Google recommends 768, 1536 or 3072; smaller = less storage

    @property
    def collection_name(self) -> str:
        """Qdrant collection: one per embedding model + size, since their vectors can't be mixed."""
        if self.qdrant_collection:
            return self.qdrant_collection
        slug = re.sub(r"[^a-z0-9]+", "-", self.embed_model.lower()).strip("-")
        return f"chunks__{slug}__{self.embed_dimensions}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
