import uuid

from app.ingest.chunking import chunk_pages, split_text
from app.ingest.parsers import ParsedPage
from app.services.vectors import chunk_point_id

SENTENCE = "A binary heap is a complete binary tree that keeps the smallest key at the root. "


def test_short_text_is_one_chunk():
    assert split_text("  Heaps are trees.  ") == ["Heaps are trees."]


def test_empty_text_has_no_chunks():
    assert split_text("   \n\n ") == []


def test_long_text_is_split_into_overlapping_pieces_of_limited_size():
    text = SENTENCE * 40  # ~3,300 characters
    pieces = split_text(text, size=800, overlap=80)

    assert len(pieces) >= 4
    assert all(len(p) <= 800 for p in pieces)
    for previous, current in zip(pieces, pieces[1:]):
        # The start of each piece repeats text from the end of the one before it.
        assert current[:20] in previous[-100:]


def test_pieces_end_at_sentence_boundaries_when_possible():
    pieces = split_text(SENTENCE * 40, size=800, overlap=80)
    assert all(p.endswith(".") for p in pieces)


def test_paragraph_break_is_preferred():
    first = "x " * 300  # 600 characters
    text = first + "\n\n" + "y " * 300
    pieces = split_text(text, size=800, overlap=0)
    assert pieces[0] == first.strip()


def test_no_words_are_lost():
    words = [f"word{i}" for i in range(600)]
    pieces = split_text(" ".join(words), size=200, overlap=30)
    seen = set(" ".join(pieces).split())
    assert set(words) <= seen


def test_one_giant_word_is_still_split():
    pieces = split_text("x" * 2000, size=800, overlap=80)
    assert all(len(p) <= 800 for p in pieces)
    assert "".join(pieces).count("x") >= 2000


def test_chunk_pages_numbers_chunks_within_each_page():
    pages = [ParsedPage(1, "short page"), ParsedPage(2, ""), ParsedPage(3, SENTENCE * 20)]
    chunks = chunk_pages(pages, slides=False)

    assert chunks[0].page_number == 1 and chunks[0].chunk_index == 0
    assert all(c.page_number != 2 for c in chunks)  # empty page: no chunks
    page3 = [c.chunk_index for c in chunks if c.page_number == 3]
    assert page3 == list(range(len(page3))) and len(page3) > 1


def test_slides_stay_whole_unless_huge():
    slide_text = SENTENCE * 20  # ~1,700 characters: too long for a PDF chunk, fine for a slide
    assert len(chunk_pages([ParsedPage(1, slide_text)], slides=True)) == 1
    assert len(chunk_pages([ParsedPage(1, slide_text)], slides=False)) > 1


def test_point_ids_are_deterministic_and_distinct():
    document_id = uuid.uuid4()
    assert chunk_point_id(document_id, 1, 0) == chunk_point_id(document_id, 1, 0)
    ids = {chunk_point_id(document_id, page, index) for page in (1, 2) for index in (0, 1)}
    assert len(ids) == 4
    assert chunk_point_id(uuid.uuid4(), 1, 0) != chunk_point_id(document_id, 1, 0)
