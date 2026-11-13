"""topics and document_topics

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name="fk_topics_course_id_courses", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_topics"),
        # Also serves lookups by course_id (its first column).
        sa.UniqueConstraint("course_id", "normalized_name", name="uq_topics_course_id"),
    )
    op.create_table(
        "document_topics",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_document_topics_document_id_documents", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"], ["topics.id"], name="fk_document_topics_topic_id_topics", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("document_id", "topic_id", "page_start", name="pk_document_topics"),
    )
    op.create_index("ix_document_topics_topic_id", "document_topics", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_document_topics_topic_id", table_name="document_topics")
    op.drop_table("document_topics")
    op.drop_table("topics")
