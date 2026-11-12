"""The Qdrant chunk index: one point per chunk, with a dense vector and a filterable payload.

Qdrant is not the source of truth: everything here can be rebuilt from the `pages` table.
Search (with its mandatory user_id + session_id filter) is added in step 3.
"""

import logging
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models

logger = logging.getLogger(__name__)

DENSE = "dense"  # meaning-based vector (Gemini embedding)
SPARSE = "sparse"  # keyword vector for hybrid search; filled in from step 3
INDEXED_FIELDS = ("user_id", "session_id", "document_id", "kind")
UPSERT_BATCH = 128

# Fixed namespace so the same chunk always gets the same point ID (see chunk_point_id).
_POINT_NAMESPACE = uuid.UUID("5b1f2c3e-8a4d-4e0f-9c6b-2d7a1e9f4c80")


def chunk_point_id(document_id: uuid.UUID, page_number: int, chunk_index: int) -> uuid.UUID:
    """Deterministic ID: re-ingesting a document overwrites its points instead of duplicating them."""
    return uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{page_number}:{chunk_index}")


def _match(field: str, values: Sequence[uuid.UUID]) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key=field, match=models.MatchAny(any=[str(v) for v in values]))]
    )


class VectorStore:
    def __init__(self, client: AsyncQdrantClient, collection: str, dimensions: int) -> None:
        self.client = client
        self.collection = collection
        self.dimensions = dimensions

    async def ensure_collection(self) -> None:
        if await self.client.collection_exists(self.collection):
            return
        try:
            await self.client.create_collection(
                self.collection,
                vectors_config={DENSE: models.VectorParams(size=self.dimensions, distance=models.Distance.COSINE)},
                sparse_vectors_config={SPARSE: models.SparseVectorParams()},
            )
        except Exception:
            # Another process (API or worker) may have created it at the same moment.
            if await self.client.collection_exists(self.collection):
                return
            raise
        for field in INDEXED_FIELDS:
            await self.client.create_payload_index(
                self.collection, field, field_schema=models.PayloadSchemaType.KEYWORD
            )
        logger.info("Created Qdrant collection %s", self.collection)

    async def upsert(self, points: list[models.PointStruct]) -> None:
        for start in range(0, len(points), UPSERT_BATCH):
            await self.client.upsert(self.collection, points=points[start : start + UPSERT_BATCH], wait=True)

    async def delete_document(self, document_id: uuid.UUID) -> None:
        await self.client.delete(
            self.collection, points_selector=models.FilterSelector(filter=_match("document_id", [document_id]))
        )

    async def delete_sessions(self, session_ids: Sequence[uuid.UUID]) -> None:
        if session_ids:
            await self.client.delete(
                self.collection, points_selector=models.FilterSelector(filter=_match("session_id", session_ids))
            )
