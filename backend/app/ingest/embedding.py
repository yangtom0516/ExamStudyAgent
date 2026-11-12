"""Text -> vectors with a Gemini embedding model.

gemini-embedding-2 takes the task as a text prefix (it has no task_type option). Documents
use "title: ... | text: ..."; search queries (step 3) will use "task: search result | query: ...".
"""

from typing import Protocol

from google.genai import types

from app.config import Settings
from app.services.gemini import make_client, with_retries

BATCH_SIZE = 50  # texts per API request


class Embedder(Protocol):
    """Anything that can embed document chunks. Tests pass a fake one, so they never call Gemini."""

    async def embed_documents(self, texts: list[str], title: str) -> list[list[float]]: ...


def document_prompt(text: str, title: str) -> str:
    return f"title: {title or 'none'} | text: {text}"


class GeminiEmbedder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # created on first use, so a missing API key fails the document, not the job setup
        self._model = settings.embed_model
        self._config = types.EmbedContentConfig(output_dimensionality=settings.embed_dimensions)

    async def embed_documents(self, texts: list[str], title: str) -> list[list[float]]:
        if self._client is None:
            self._client = make_client(self._settings)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            # One Content per text: given a plain list, gemini-embedding-2 would return a
            # single combined embedding instead of one per text.
            contents = [
                types.Content(parts=[types.Part.from_text(text=document_prompt(text, title))]) for text in batch
            ]
            result = await with_retries(
                lambda: self._client.aio.models.embed_content(
                    model=self._model, contents=contents, config=self._config
                ),
                what=f"Embedding {len(batch)} chunks",
            )
            embeddings = result.embeddings or []
            if len(embeddings) != len(batch):
                raise RuntimeError(f"Expected {len(batch)} embeddings, got {len(embeddings)}")
            vectors.extend(list(e.values) for e in embeddings)
        return vectors
