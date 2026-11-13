"""Hybrid search over a study session's chunks (lectures and notes; past exams aren't indexed).

Two searches run on the same filtered set of chunks:
- by meaning: the query's embedding vs. the chunks' embeddings (finds paraphrases);
- by keywords: the query's words vs. the chunks' words, rare words counting more (finds exact
  terms like "Theorem 3.2").
Qdrant merges the two ranked lists with RRF: a chunk ranked high in either list comes out high.

The user_id + session_id filter is always added here; callers (including the AI agents) can only
narrow it further with kind/topic, never widen it.
"""

import uuid

from pydantic import BaseModel
from qdrant_client import models

from app.ingest.embedding import Embedder
from app.ingest.topics import normalize_topic_name
from app.services import keywords
from app.services.vectors import DENSE, SPARSE, VectorStore

CANDIDATES_PER_SEARCH = 50  # how many each of the two searches contributes before merging


class SearchHit(BaseModel):
    document_id: str
    filename: str
    page_number: int
    topics: list[str]
    text: str
    score: float  # RRF score: only meaningful for ordering


def _filter(user_id: uuid.UUID, session_id: uuid.UUID, kind: str | None, topic: str | None) -> models.Filter:
    conditions = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=str(user_id))),
        models.FieldCondition(key="session_id", match=models.MatchValue(value=str(session_id))),
    ]
    if kind:
        conditions.append(models.FieldCondition(key="kind", match=models.MatchValue(value=kind)))
    if topic:
        # `topics` is a list; this matches chunks whose list contains the topic.
        conditions.append(
            models.FieldCondition(key="topics", match=models.MatchValue(value=normalize_topic_name(topic)))
        )
    return models.Filter(must=conditions)


async def search(
    vectors: VectorStore,
    embedder: Embedder,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    query: str,
    kind: str | None = None,
    topic: str | None = None,
    limit: int = 8,
) -> list[SearchHit]:
    where = _filter(user_id, session_id, kind, topic)
    prefetch = [
        models.Prefetch(
            query=await embedder.embed_query(query), using=DENSE, filter=where, limit=CANDIDATES_PER_SEARCH
        )
    ]
    words = keywords.query_vector(query)
    if words.indices:  # a query of only punctuation has no words to match
        prefetch.append(models.Prefetch(query=words, using=SPARSE, filter=where, limit=CANDIDATES_PER_SEARCH))

    result = await vectors.client.query_points(
        vectors.collection,
        prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=where,
        limit=limit,
        with_payload=True,
    )
    return [
        SearchHit(
            document_id=point.payload["document_id"],
            filename=point.payload.get("filename", ""),
            page_number=point.payload["page_number"],
            topics=point.payload.get("topics", []),
            text=point.payload["text"],
            score=point.score,
        )
        for point in result.points
    ]
