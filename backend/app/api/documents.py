"""Uploading and managing a session's files (PDF / PPTX). Files can only change while the session is in setup.

Reading the files (parsing, chunking, indexing) happens later in the Celery worker; an upload
only stores the file and creates a `queued` document row.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import DbSession, OwnedDocument, OwnedSession, SettingsDep, StorageDep, require_setup
from app.db.models import Document, DocumentKind, DocumentStatus, StudySession
from app.services.storage import document_key

router = APIRouter(tags=["documents"])
logger = logging.getLogger(__name__)

# Extension -> (content type, the bytes every such file starts with).
# Checking the first bytes catches files that are merely renamed (e.g. a .docx called .pdf).
# PPTX files are ZIP archives, so they start with the ZIP signature.
ALLOWED_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", b"PK\x03\x04"),
}


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    filename: str
    kind: DocumentKind
    status: DocumentStatus
    error: str | None
    page_count: int | None
    size_bytes: int
    created_at: datetime


def _clean_filename(raw: str | None) -> str:
    # Keep only the last path part (some browsers send "C:\fakepath\notes.pdf").
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File has no name")
    return name[-255:]


@router.post(
    "/sessions/{session_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentOut,
)
async def upload_document(
    study_session: OwnedSession,
    file: Annotated[UploadFile, File(description="PDF or PPTX")],
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    kind: Annotated[DocumentKind, Form()] = DocumentKind.LECTURE,
):
    """Store the file and queue it for processing. Returns 202: processing happens in the background."""
    require_setup(study_session)

    filename = _clean_filename(file.filename)
    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if extension not in ALLOWED_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF and PPTX files are accepted")
    content_type, signature = ALLOWED_TYPES[extension]

    # The upload has already been received into a temporary file at this point. In production a
    # reverse-proxy body limit (Caddy) stops huge uploads before they reach the app.
    size = file.size or 0
    if size == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "File is empty")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"File is larger than {settings.max_upload_mb} MB"
        )

    first_bytes = await file.read(len(signature))
    await file.seek(0)
    if first_bytes != signature:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"File content is not a valid {extension[1:].upper()}"
        )

    document_id = uuid.uuid4()
    key = document_key(study_session.id, document_id, extension)
    await storage.upload(key, file.file, content_type)

    document = Document(
        id=document_id,
        session_id=study_session.id,
        filename=filename,
        storage_key=key,
        kind=kind,
        status=DocumentStatus.QUEUED,
        size_bytes=size,
    )
    db.add(document)
    try:
        await db.commit()
    except Exception:
        await storage.delete(key)  # don't leave a file with no row pointing at it
        raise
    await db.refresh(document)

    # TODO(step 2c): ingest_document.delay(document.id)
    return document


@router.get("/sessions/{session_id}/documents", response_model=list[DocumentOut])
async def list_documents(study_session: OwnedSession, db: DbSession):
    documents = await db.scalars(
        select(Document)
        .where(Document.session_id == study_session.id)
        .order_by(Document.created_at, Document.id)
    )
    return documents.all()


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document: OwnedDocument, db: DbSession, storage: StorageDep) -> None:
    study_session = await db.get(StudySession, document.session_id)
    require_setup(study_session)

    key = document.storage_key
    await db.delete(document)
    await db.commit()
    # TODO(step 2c): also delete the document's chunks from Qdrant.
    try:
        await storage.delete(key)
    except Exception:
        # The row is gone, so the file is unreachable; log it rather than fail the request.
        logger.exception("Could not delete stored file %s", key)
