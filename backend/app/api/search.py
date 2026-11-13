"""Try-it search over a session's material: the same hybrid search ExamWriter will use."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, EmbedderDep, OwnedSession, VectorsDep
from app.services.search import SearchHit, search

router = APIRouter(tags=["search"])


@router.get("/sessions/{session_id}/search", response_model=list[SearchHit])
async def search_session(
    study_session: OwnedSession,
    user: CurrentUser,
    vectors: VectorsDep,
    embedder: EmbedderDep,
    q: Annotated[str, Query(min_length=1, max_length=500, description="What to look for")],
    kind: Literal["lecture", "notes"] | None = None,
    topic: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
):
    """Best-matching chunks of this session's lectures and notes, with file name and page.
    Costs one embedding call per search."""
    return await search(
        vectors, embedder, user_id=user.id, session_id=study_session.id, query=q, kind=kind, topic=topic, limit=limit
    )
