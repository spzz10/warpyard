"""bring-your-own-key AI agent: users.llm_provider/llm_api_key + ai_messages table

Revision ID: j0d1e2f3a4b5
Revises: i9c0d1e2f3a4
Create Date: 2026-07-13 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "i9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("llm_provider", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("llm_api_key", sa.String(length=200), nullable=True))
    op.create_table(
        "ai_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("ai_messages")
    op.drop_column("users", "llm_api_key")
    op.drop_column("users", "llm_provider")
