import pytest

from app.config import get_settings
from tests.helpers import PDF_BYTES, PPTX_BYTES, create_course, create_session, upload, upload_ok


@pytest.fixture
def session(client, login_as):
    """Alice is logged in and has an empty session in setup status."""
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def storage_key(db_fetch_one, document_id):
    return db_fetch_one("SELECT storage_key FROM documents WHERE id = :id", id=document_id)["storage_key"]


# --- uploading --------------------------------------------------------------


def test_upload_pdf_is_stored_and_queued(client, session, db_fetch_one, stored_file_exists):
    document = upload_ok(client, session["id"], filename="Lecture 1.pdf")

    assert document["filename"] == "Lecture 1.pdf"
    assert document["kind"] == "lecture"  # the default
    assert document["status"] == "queued"
    assert document["size_bytes"] == len(PDF_BYTES)
    assert document["session_id"] == session["id"]

    key = storage_key(db_fetch_one, document["id"])
    assert key == f"sessions/{session['id']}/{document['id']}.pdf"
    assert stored_file_exists(key)


def test_upload_pptx_and_choose_kind(client, session):
    document = upload_ok(client, session["id"], filename="week2.PPTX", content=PPTX_BYTES, kind="notes")
    assert document["kind"] == "notes"


def test_list_documents(client, session):
    upload_ok(client, session["id"], filename="a.pdf")
    upload_ok(client, session["id"], filename="b.pdf", kind="past_exam")

    listed = client.get(f"/sessions/{session['id']}/documents").json()
    assert [(d["filename"], d["kind"]) for d in listed] == [("a.pdf", "lecture"), ("b.pdf", "past_exam")]


def test_browser_fake_path_is_stripped_from_filename(client, session):
    document = upload_ok(client, session["id"], filename="C:\\fakepath\\slides.pdf")
    assert document["filename"] == "slides.pdf"


@pytest.mark.parametrize(
    "filename, content",
    [
        ("essay.docx", b"PK\x03\x04rest"),  # wrong extension
        ("noextension", PDF_BYTES),
        ("renamed.pdf", b"just some text"),  # .pdf name, not PDF content
        ("renamed.pptx", PDF_BYTES),  # .pptx name, PDF content
    ],
)
def test_wrong_file_types_are_rejected(client, session, filename, content):
    assert upload(client, session["id"], filename=filename, content=content).status_code == 415


def test_empty_file_is_rejected(client, session):
    assert upload(client, session["id"], content=b"").status_code == 422


def test_unknown_kind_is_rejected(client, session):
    assert upload(client, session["id"], kind="homework").status_code == 422


def test_file_over_size_limit_is_rejected(client, session):
    # Lower the limit to 1 MB for this test instead of sending 50 MB.
    small_limit = get_settings().model_copy(update={"max_upload_mb": 1})
    client.app.dependency_overrides[get_settings] = lambda: small_limit
    try:
        too_big = PDF_BYTES + b"0" * (1024 * 1024)
        response = upload(client, session["id"], content=too_big)
    finally:
        client.app.dependency_overrides.clear()
    assert response.status_code == 413


def test_upload_to_started_session_is_rejected(client, session, db_execute):
    db_execute("UPDATE study_sessions SET status = 'active' WHERE id = :id", id=session["id"])
    response = upload(client, session["id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "Session already started; its files are locked"


# --- deleting ---------------------------------------------------------------


def test_delete_document_removes_row_and_file(client, session, db_fetch_one, stored_file_exists):
    document = upload_ok(client, session["id"])
    key = storage_key(db_fetch_one, document["id"])

    assert client.delete(f"/documents/{document['id']}").status_code == 204
    assert client.get(f"/sessions/{session['id']}/documents").json() == []
    assert not stored_file_exists(key)


def test_delete_document_in_started_session_is_rejected(client, session, db_execute):
    document = upload_ok(client, session["id"])
    db_execute("UPDATE study_sessions SET status = 'active' WHERE id = :id", id=session["id"])
    assert client.delete(f"/documents/{document['id']}").status_code == 409


def test_deleting_session_removes_its_files(client, session, db_fetch_one, stored_file_exists):
    key = storage_key(db_fetch_one, upload_ok(client, session["id"])["id"])

    assert client.delete(f"/sessions/{session['id']}").status_code == 204
    assert not stored_file_exists(key)


def test_deleting_course_removes_its_files(client, session, db_fetch_one, stored_file_exists):
    key = storage_key(db_fetch_one, upload_ok(client, session["id"])["id"])

    assert client.delete(f"/courses/{session['course_id']}").status_code == 204
    assert not stored_file_exists(key)


# --- ownership ---------------------------------------------------------------


def test_other_users_cannot_see_upload_or_delete(client, session, login_as, db_fetch_one, stored_file_exists):
    document = upload_ok(client, session["id"])
    key = storage_key(db_fetch_one, document["id"])

    login_as("bob")
    assert client.get(f"/sessions/{session['id']}/documents").status_code == 404
    assert upload(client, session["id"]).status_code == 404
    assert client.delete(f"/documents/{document['id']}").status_code == 404
    assert stored_file_exists(key)

    login_as("alice")
    assert len(client.get(f"/sessions/{session['id']}/documents").json()) == 1


def test_upload_requires_login(client, session):
    client.cookies.clear()
    assert upload(client, session["id"]).status_code == 401
