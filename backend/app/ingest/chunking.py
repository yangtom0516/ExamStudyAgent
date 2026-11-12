"""Split pages into chunks, the units that get embedded and searched.

- PDF pages: one chunk per page; long pages are split into ~800-character pieces that
  overlap by ~80 characters, so a sentence cut at a boundary still appears whole in one piece.
- Slides: one chunk per slide (text + speaker notes). Only very long slides are split.
- Past exams are chunked like PDF pages for now; one chunk per question comes in step 3.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.ingest.parsers import ParsedPage

PAGE_CHUNK_CHARS = 800
CHUNK_OVERLAP_CHARS = 80
SLIDE_MAX_CHARS = 4000

# Preferred places to cut, best first: paragraph break, line break, end of sentence, space.
_SEPARATORS = ("\n\n", "\n", ". ", " ")


@dataclass(frozen=True)
class Chunk:
    page_number: int
    chunk_index: int  # position within its page: 0, 1, 2...
    text: str


def _cut_position(text: str, start: int, end: int) -> int:
    """Where to end the piece text[start:end]: the last good separator in the window's second half."""
    floor = start + (end - start) // 2
    for separator in _SEPARATORS:
        found = text.rfind(separator, floor, end)
        if found != -1:
            return found + len(separator)
    return end  # no separator at all (one very long "word"): cut mid-word


def split_text(text: str, size: int = PAGE_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Split text into pieces of at most `size` characters, each overlapping the previous one."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            end = _cut_position(text, start, end)
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        # Step back `overlap` characters, then forward to the start of a word.
        next_start = max(end - overlap, start + 1)
        while next_start < end and not text[next_start - 1].isspace():
            next_start += 1
        start = next_start
    return pieces


def chunk_pages(pages: Iterable[ParsedPage], *, slides: bool) -> list[Chunk]:
    size = SLIDE_MAX_CHARS if slides else PAGE_CHUNK_CHARS
    return [
        Chunk(page.number, index, piece)
        for page in pages
        for index, piece in enumerate(split_text(page.text, size))
    ]
