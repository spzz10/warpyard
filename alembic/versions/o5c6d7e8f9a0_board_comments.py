"""board_comments — comment section under each share-board listing

Revision ID: o5c6d7e8f9a0
Revises: n4b5c6d7e8f9
Create Date: 2026-07-13 23:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "o5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "n4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "board_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_board_comments_instance_id", "board_comments", ["instance_id"])


def downgrade() -> None:
    op.drop_index("ix_board_comments_instance_id", table_name="board_comments")
    op.drop_table("board_comments")
