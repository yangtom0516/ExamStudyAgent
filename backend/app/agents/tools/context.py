"""What every agent tool needs: the app's services, and who is asking.

Identity (user, course, session) is put into the ADK session state by the server before an agent
runs (`initial_state`), and tools read it from `tool_context.state`. No tool takes an id as an
argument, so the model can choose *what* to search or save, never *whose* data.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Course, StudySession
from app.ingest.embedding import Embedder
from app.services.vectors import VectorStore

# Keys in the ADK session state. Values are strings: ADK may store state as JSON.
USER_ID = "user_id"
COURSE_ID = "course_id"
SESSION_ID = "session_id"
REQUEST = "request"  # what the student asked for, word for word (saved with the exam)
LAST_EXAM_ID = "last_exam_id"  # set by save_exam; the chat stream (step 6) announces the new exam


@dataclass(frozen=True)
class AgentServices:
    """Connections the tools use, created once at startup (or by a test/script)."""

    sessionmaker: async_sessionmaker[AsyncSession]
    vectors: VectorStore
    embedder: Embedder


@dataclass(frozen=True)
class Identity:
    user_id: uuid.UUID
    course_id: uuid.UUID
    session_id: uuid.UUID


class MissingIdentityError(Exception):
    """The session state lacks the ids the server should have put there (a wiring bug)."""


def initial_state(*, user_id: uuid.UUID, course_id: uuid.UUID, session_id: uuid.UUID, request: str) -> dict:
    """The session state the server sets before running an agent."""
    return {USER_ID: str(user_id), COURSE_ID: str(course_id), SESSION_ID: str(session_id), REQUEST: request}


def identity_from(tool_context) -> Identity:
    state = tool_context.state
    try:
        return Identity(
            user_id=uuid.UUID(str(state[USER_ID])),
            course_id=uuid.UUID(str(state[COURSE_ID])),
            session_id=uuid.UUID(str(state[SESSION_ID])),
        )
    except (KeyError, ValueError) as exc:
        raise MissingIdentityError("This chat isn't linked to a study session; nothing can be read or saved.") from exc


def tool_error(message: str) -> dict:
    """What a tool returns when it can't do its job; the model reads `error` and can react."""
    return {"ok": False, "error": message}


async def session_is_theirs(db: AsyncSession, who: Identity) -> bool:
    """Defense in depth: the session in the state must belong to the user (and course) in the state."""
    found = await db.scalar(
        select(StudySession.id)
        .join(Course, StudySession.course_id == Course.id)
        .where(
            StudySession.id == who.session_id,
            StudySession.course_id == who.course_id,
            Course.user_id == who.user_id,
        )
    )
    return found is not None
