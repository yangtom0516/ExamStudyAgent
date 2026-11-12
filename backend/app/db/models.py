"""Database tables. Every change here needs a matching Alembic migration in alembic/versions/."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    # Random UUIDs instead of 1, 2, 3... so IDs in URLs don't reveal how many users exist.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Stored lowercase; 3-30 characters is enforced by the API.
    username: Mapped[str] = mapped_column(String(30), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Deleting a user deletes their courses (and, through the chain, everything below).
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionStatus(StrEnum):
    SETUP = "setup"  # files can be uploaded and deleted
    ACTIVE = "active"  # file list locked; chat and exams allowed


class StudySession(Base):
    """A study session ("Midterm 1 prep") inside a course. It owns its files.

    Named study_sessions, not sessions: Google ADK's session store creates its own `sessions` table.
    """

    __tablename__ = "study_sessions"
    __table_args__ = (CheckConstraint("status IN ('setup', 'active')", name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default=SessionStatus.SETUP, server_default="setup")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentKind(StrEnum):
    LECTURE = "lecture"
    PAST_EXAM = "past_exam"
    NOTES = "notes"


class DocumentStatus(StrEnum):
    QUEUED = "queued"  # uploaded, waiting for the ingestion worker
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"  # see `error`; the user can retry


class Document(Base):
    """An uploaded PDF or PPTX. The file itself lives in object storage under `storage_key`."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("kind IN ('lecture', 'past_exam', 'notes')", name="kind"),
        CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))  # original name, for display only
    storage_key: Mapped[str] = mapped_column(String(512))  # sessions/<session_id>/<document_id>.<ext>
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.QUEUED, server_default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
