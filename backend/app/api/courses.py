import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import distinct, func, select

from app.api.deps import CurrentUser, DbSession, OwnedCourse, StorageDep, VectorsDep
from app.api.sessions import delete_session_data
from app.db.models import Course, Document, DocumentStatus, DocumentTopic, StudySession, Topic

router = APIRouter(prefix="/courses", tags=["courses"])

CourseName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class CourseCreate(BaseModel):
    name: CourseName


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime


@router.get("", response_model=list[CourseOut])
async def list_courses(user: CurrentUser, db: DbSession):
    """The logged-in user's courses, oldest first."""
    courses = await db.scalars(
        select(Course).where(Course.user_id == user.id).order_by(Course.created_at, Course.id)
    )
    return courses.all()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CourseOut)
async def create_course(body: CourseCreate, user: CurrentUser, db: DbSession):
    course = Course(user_id=user.id, name=body.name)
    db.add(course)
    await db.commit()
    await db.refresh(course)  # load created_at, set by the database
    return course


class TopicOut(BaseModel):
    id: uuid.UUID
    name: str
    documents: int  # ready documents covering the topic, in any session of the course
    pages: int  # pages of those documents covering it


@router.get("/{course_id}/topics", response_model=list[TopicOut])
async def list_topics(course: OwnedCourse, db: DbSession):
    """The course's topics and how much material covers each. Topics stay after their documents are
    deleted (with 0 coverage), so scores can carry over between sessions."""
    coverage = (
        select(
            DocumentTopic.topic_id,
            func.count(distinct(DocumentTopic.document_id)).label("documents"),
            func.sum(DocumentTopic.page_end - DocumentTopic.page_start + 1).label("pages"),
        )
        .join(Document, DocumentTopic.document_id == Document.id)
        .where(Document.status == DocumentStatus.READY)
        .group_by(DocumentTopic.topic_id)
        .subquery()
    )
    rows = await db.execute(
        select(
            Topic.id,
            Topic.name,
            func.coalesce(coverage.c.documents, 0),
            func.coalesce(coverage.c.pages, 0),
        )
        .outerjoin(coverage, coverage.c.topic_id == Topic.id)
        .where(Topic.course_id == course.id)
        .order_by(Topic.name)
    )
    return [TopicOut(id=id_, name=name, documents=docs, pages=pages) for id_, name, docs, pages in rows.all()]


@router.get("/{course_id}", response_model=CourseOut)
async def get_course(course: OwnedCourse):
    return course


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course(course: OwnedCourse, db: DbSession, storage: StorageDep, vectors: VectorsDep) -> None:
    """Delete the course, its sessions and documents (ON DELETE CASCADE), stored files and Qdrant chunks."""
    session_ids = list(await db.scalars(select(StudySession.id).where(StudySession.course_id == course.id)))
    await db.delete(course)
    await db.commit()
    await delete_session_data(storage, vectors, session_ids)
