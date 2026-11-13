import io
import zipfile

import pytest

from app.ingest.parsers import (
    UnreadableFileError,
    clean_text,
    looks_garbled,
    parse_document,
    parse_pdf,
    parse_pptx,
    split_pdf_pages,
)
from tests.helpers import make_encrypted_pdf, make_pdf, make_pptx, make_pptx_with_table, unreadable_font


def test_pdf_pages_are_read_in_order():
    # (No apostrophes: in the test PDF's font, ' is defined as the curly quote ’.)
    pages = parse_pdf(make_pdf(["Heaps and priority queues", "", "Dijkstra shortest paths"]))

    assert [p.number for p in pages] == [1, 2, 3]
    assert "Heaps and priority queues" in pages[0].text
    assert pages[1].text == ""  # no text layer, like a scanned page
    assert "Dijkstra shortest paths" in pages[2].text


def test_only_scanned_looking_pages_are_flagged_for_ocr():
    pages = parse_pdf(
        make_pdf(
            ["", "12", "", "Heaps are complete binary trees", "Short"],
            image_pages=[1, 2, 4],  # page 3: blank divider (no image); page 5: little text, no image
        )
    )
    assert [p.needs_ocr for p in pages] == [
        True,  # image, no text: a scan
        True,  # image + stamped page number: still a scan
        False,  # blank page without an image: nothing to read
        False,  # real text (plus an image): no OCR needed
        False,  # little text but no image
    ]
    assert all(p.source == "text" for p in pages)


def test_text_in_an_unreadable_font_goes_to_ocr_instead():
    pages = parse_pdf(make_pdf([unreadable_font("Split Brain: the primary and backup disagree"), "Heaps"]))

    assert pages[0].needs_ocr and pages[0].text == ""  # the scrambled text is never kept
    assert not pages[1].needs_ocr and pages[1].text == "Heaps"


def test_looks_garbled():
    assert looks_garbled("6SOLW\x03%UDLQ\x03LV\x03EDG")  # "Split Brain is bad"
    assert not looks_garbled("Split Brain\nis bad\n\nreally")  # newlines are normal
    assert not looks_garbled("A long normal paragraph with one stray \x02 control character in it")
    assert not looks_garbled("")


def test_split_pdf_pages_makes_one_page_pdfs():
    data = make_pdf(["first", "second", "third"])
    parts = split_pdf_pages(data, [3, 1])

    assert sorted(parts) == [1, 3]
    assert [p.text for p in parse_pdf(parts[3])] == ["third"]
    assert [p.text for p in parse_pdf(parts[1])] == ["first"]


def test_password_protected_pdf_is_unreadable():
    with pytest.raises(UnreadableFileError, match="password-protected"):
        parse_pdf(make_encrypted_pdf())


def test_damaged_pdf_is_unreadable():
    with pytest.raises(UnreadableFileError, match="damaged"):
        parse_pdf(b"%PDF-1.4\nthis is not really a pdf")


def test_pptx_slides_include_speaker_notes():
    data = make_pptx([("Heaps", "Min-heap property", "Mention the array layout"), ("Graphs", "BFS and DFS", "")])
    pages = parse_pptx(data)

    assert [p.number for p in pages] == [1, 2]
    assert "Heaps" in pages[0].text and "Min-heap property" in pages[0].text
    assert "Speaker notes:\nMention the array layout" in pages[0].text
    assert "Speaker notes" not in pages[1].text


def test_pptx_table_cells_are_read():
    pages = parse_pptx(make_pptx_with_table([["Operation", "Cost"], ["push", "O(log n)"]]))
    assert "push | O(log n)" in pages[0].text


def test_other_zip_files_renamed_to_pptx_are_unreadable():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<doc/>")
    with pytest.raises(UnreadableFileError, match="not a valid PPTX"):
        parse_pptx(buffer.getvalue())


def test_parse_document_picks_parser_by_extension():
    assert parse_document(make_pdf(["hello"]), ".pdf")[0].text == "hello"
    assert "Title" in parse_document(make_pptx([("Title", "", "")]), ".pptx")[0].text


def test_clean_text_tidies_whitespace_and_removes_nul():
    assert clean_text("  a\x00b \t c\r\n\n\n\n  d  ") == "ab c\n\nd"
