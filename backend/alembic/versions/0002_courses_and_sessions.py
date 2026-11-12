"""courses and study sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_courses_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_courses"),
    )
    op.create_index("ix_courses_user_id", "courses", ["user_id"])

    op.create_table(
        "study_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="setup", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('setup', 'active')", name="ck_study_sessions_status"),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name="fk_study_sessions_course_id_courses", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_study_sessions"),
    )
    op.create_index("ix_study_sessions_course_id", "study_sessions", ["course_id"])


def downgrade() -> None:
    op.drop_index("ix_study_sessions_course_id", table_name="study_sessions")
    op.drop_table("study_sessions")
    op.drop_index("ix_courses_user_id", table_name="courses")
    op.drop_table("courses")
