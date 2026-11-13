"""Chunks -> embeddings + keyword vectors -> Qdrant. Shared by ingestion and by reindex.

Each point carries two vectors (meaning and keywords) and the payload used for filtering and
citations. A document's old points are removed first, in case an earlier run made more chunks.
"""

import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from qdrant_client import models

from app.ingest.chunking import Chunk
from app.ingest.embedding import Embedder
from app.services import keywords
from app.services.vectors import DENSE, SPARSE, VectorStore, chunk_point_id


@dataclass(frozen=True)
class DocumentInfo:
    """What indexing needs to know about a document, copied out of the database."""

    document_id: uuid.UUID
    session_id: uuid.UUID
    course_id: uuid.UUID
    user_id: uuid.UUID
    filename: str
    storage_key: str
    kind: str

    @property
    def extension(self) -> str:
        return PurePosixPath(self.storage_key).suffix

    @property
    def title(self) -> str:
        return PurePosixPath(self.filename).stem


async def index_chunks(
    doc: DocumentInfo,
    chunks: list[Chunk],
    page_topics: dict[int, list[str]],
    embedder: Embedder,
    vectors: VectorStore,
) -> None:
    embeddings = await embedder.embed_documents([c.text for c in chunks], title=doc.title)
    shared = {
        "user_id": str(doc.user_id),
        "course_id": str(doc.course_id),
        "session_id": str(doc.session_id),
        "document_id": str(doc.document_id),
        "filename": doc.filename,  # for citations: "Lecture 5.pdf, page 12"
        "kind": doc.kind,
    }
    points = [
        models.PointStruct(
            id=str(chunk_point_id(doc.document_id, chunk.page_number, chunk.chunk_index)),
            vector={DENSE: embedding, SPARSE: keywords.document_vector(chunk.text)},
            payload={
                **shared,
                "page_number": chunk.page_number,
                "chunk_index": chunk.chunk_index,
                "topics": page_topics.get(chunk.page_number, []),  # normalized names
                "text": chunk.text,
            },
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    await vectors.delete_document(doc.document_id)
    await vectors.upsert(points)
