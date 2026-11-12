import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, OwnedCourse, StorageDep, VectorsDep
from app.api.sessions import delete_session_data
from app.db.models import Course, StudySession

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
