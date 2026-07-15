"""Remove the in-app AI assistant: drop conversations + ai_messages tables and
users.llm_provider/llm_api_key. The platform now leans on MCP + API/SSH for AI-driven
control instead of a bring-your-own-key chat assistant.

Revision ID: l2f3a4b5c6d7
Revises: k1e2f3a4b5c6
Create Date: 2026-07-13 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "l2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "k1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("ai_messages")  # cascades its conversation_id index
    op.drop_table("conversations")
    op.drop_column("users", "llm_api_key")
    op.drop_column("users", "llm_provider")


def downgrade() -> None:
    op.add_column("users", sa.Column("llm_provider", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("llm_api_key", sa.String(length=200), nullable=True))
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("title", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "ai_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_messages_conversation_id", "ai_messages", ["conversation_id"])
