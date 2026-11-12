import io
import zipfile

import pytest

from app.ingest.parsers import UnreadableFileError, clean_text, parse_document, parse_pdf, parse_pptx
from tests.helpers import make_encrypted_pdf, make_pdf, make_pptx, make_pptx_with_table


def test_pdf_pages_are_read_in_order():
    # (No apostrophes: in the test PDF's font, ' is defined as the curly quote ’.)
    pages = parse_pdf(make_pdf(["Heaps and priority queues", "", "Dijkstra shortest paths"]))

    assert [p.number for p in pages] == [1, 2, 3]
    assert "Heaps and priority queues" in pages[0].text
    assert pages[1].text == ""  # no text layer, like a scanned page
    assert "Dijkstra shortest paths" in pages[2].text


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
