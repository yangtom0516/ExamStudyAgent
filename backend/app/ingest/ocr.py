"""OCR: read the text of a scanned PDF page with Gemini.

The page is sent as a one-page PDF together with the instructions in prompts/ocr.md. Gemini is
multimodal: it "looks" at the page and returns the text it reads.
"""

from pathlib import Path
from typing import Protocol

from google.genai import types

from app.config import Settings
from app.ingest.parsers import clean_text
from app.services.gemini import make_client, with_retries

OCR_PROMPT = (Path(__file__).parent / "prompts" / "ocr.md").read_text(encoding="utf-8")


class PageReader(Protocol):
    """Anything that can read a one-page PDF. Tests pass a fake one, so they never call Gemini."""

    async def read_page(self, page_pdf: bytes) -> str: ...


class GeminiOcr:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # created on first use, like the embedder
        self._model = settings.ocr_model

    async def read_page(self, page_pdf: bytes) -> str:
        if self._client is None:
            self._client = make_client(self._settings)
        contents = [types.Part.from_bytes(data=page_pdf, mime_type="application/pdf"), OCR_PROMPT]
        response = await with_retries(
            lambda: self._client.aio.models.generate_content(model=self._model, contents=contents),
            what="OCR of one page",
        )
        return clean_text(response.text or "")  # no text (e.g. a blocked response) -> ""
