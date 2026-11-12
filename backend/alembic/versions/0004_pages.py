"""pages

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=10), server_default="text", nullable=False),
        sa.CheckConstraint("source IN ('text', 'ocr')", name="ck_pages_source"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_pages_document_id_documents", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pages"),
        sa.UniqueConstraint("document_id", "page_number", name="uq_pages_document_id"),
    )


def downgrade() -> None:
    op.drop_table("pages")
