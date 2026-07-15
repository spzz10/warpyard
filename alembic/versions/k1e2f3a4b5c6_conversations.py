"""AI assistant saved conversations: conversations table + ai_messages.conversation_id

Revision ID: k1e2f3a4b5c6
Revises: j0d1e2f3a4b5
Create Date: 2026-07-13 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'k1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'j0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'conversations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('title', sa.String(length=80), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column('ai_messages', sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('conversations.id'), nullable=True))
    op.create_index('ix_ai_messages_conversation_id', 'ai_messages', ['conversation_id'])

    # Data migration: bucket each user's existing flat messages into one "Earlier chat"
    # conversation so nothing is lost when the UI switches to per-conversation history.
    conn = op.get_bind()
    user_ids = [r[0] for r in conn.execute(sa.text('SELECT DISTINCT user_id FROM ai_messages WHERE conversation_id IS NULL'))]
    for uid in user_ids:
        conv_id = conn.execute(
            sa.text(
                "INSERT INTO conversations (user_id, title, created_at, updated_at) "
                "VALUES (:uid, 'Earlier chat', now(), now()) RETURNING id"
            ),
            {'uid': uid},
        ).scalar()
        conn.execute(
            sa.text('UPDATE ai_messages SET conversation_id = :cid WHERE user_id = :uid AND conversation_id IS NULL'),
            {'cid': conv_id, 'uid': uid},
        )


def downgrade() -> None:
    op.drop_index('ix_ai_messages_conversation_id', table_name='ai_messages')
    op.drop_column('ai_messages', 'conversation_id')
    op.drop_table('conversations')
