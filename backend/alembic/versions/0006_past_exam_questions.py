"""past_exam_questions

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "past_exam_questions",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("points", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "type IN ('multiple_choice', 'short_answer', 'long_answer', 'other')",
            name="ck_past_exam_questions_type",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_past_exam_questions_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "position", name="pk_past_exam_questions"),
    )


def downgrade() -> None:
    op.drop_table("past_exam_questions")
