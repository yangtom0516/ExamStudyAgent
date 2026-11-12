"""POST /sessions/{id}/start and the file lock it creates."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.services.storage import Storage
from tests.helpers import create_course, create_session, make_pdf, upload, upload_ok


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def start(client, session_id):
    return client.post(f"/sessions/{session_id}/start")


def test_start_when_every_file_is_ready(client, session, run_ingest):
    for name in ("a.pdf", "b.pdf"):
        run_ingest(upload_ok(client, session["id"], filename=name, content=make_pdf(["Heaps"]))["id"])

    response = start(client, session["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["started_at"] is not None
    assert client.get(f"/sessions/{session['id']}").json()["status"] == "active"


def test_cannot_start_without_files(client, session):
    response = start(client, session["id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "Upload at least one file first"


def test_cannot_start_while_files_are_processing(client, session, run_ingest):
    run_ingest(upload_ok(client, session["id"], content=make_pdf(["Heaps"]))["id"])
    upload_ok(client, session["id"], content=make_pdf(["Graphs"]))  # still queued

    response = start(client, session["id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "1 file still processing"
    assert client.get(f"/sessions/{session['id']}").json()["status"] == "setup"


def test_cannot_start_with_failed_files(client, session, run_ingest):
    run_ingest(upload_ok(client, session["id"], content=make_pdf(["", ""]))["id"])  # no text: fails

    response = start(client, session["id"])
    assert response.status_code == 409
    assert response.json()["detail"] == "1 file failed; retry or delete them first"


def test_cannot_start_twice(client, session, run_ingest):
    run_ingest(upload_ok(client, session["id"], content=make_pdf(["Heaps"]))["id"])
    assert start(client, session["id"]).status_code == 200
    assert start(client, session["id"]).status_code == 409


def test_started_session_locks_its_files(client, session, run_ingest):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    run_ingest(document["id"])
    start(client, session["id"])

    assert upload(client, session["id"]).status_code == 409
    assert client.delete(f"/documents/{document['id']}").status_code == 409
    assert len(client.get(f"/sessions/{session['id']}/documents").json()) == 1  # still listed


def test_other_users_cannot_start_my_session(client, session, login_as):
    login_as("bob")
    assert start(client, session["id"]).status_code == 404


def test_upload_rechecks_the_session_after_storing_the_file(
    client, session, run_ingest, monkeypatch, stored_file_exists
):
    """The race from the design: the session is started while a file is still uploading.

    The upload passed its first check, but the re-check under the session lock must refuse it
    and remove the file it stored.
    """
    run_ingest(upload_ok(client, session["id"], content=make_pdf(["Heaps"]))["id"])
    real_upload = Storage.upload
    stored: list[str] = []

    async def upload_while_session_is_started(self, key, fileobj, content_type):
        await real_upload(self, key, fileobj, content_type)
        stored.append(key)
        # Meanwhile, in another request, the user presses Start.
        engine = create_async_engine(get_settings().database_url)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE study_sessions SET status = 'active' WHERE id = :id"), {"id": session["id"]}
            )
        await engine.dispose()

    monkeypatch.setattr(Storage, "upload", upload_while_session_is_started)
    response = upload(client, session["id"], filename="late.pdf", content=make_pdf(["Late"]))

    assert response.status_code == 409
    assert not stored_file_exists(stored[0])  # the stored file was cleaned up
    filenames = [d["filename"] for d in client.get(f"/sessions/{session['id']}/documents").json()]
    assert "late.pdf" not in filenames
