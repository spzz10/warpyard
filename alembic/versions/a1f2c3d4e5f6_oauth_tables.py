"""oauth tables (clients, codes, tokens) for the MCP authorization server

Revision ID: a1f2c3d4e5f6
Revises: 622113986623
Create Date: 2026-07-11 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '622113986623'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'oauth_clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.String(length=48), nullable=False),
        sa.Column('client_name', sa.String(length=128), nullable=True),
        sa.Column('redirect_uris', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_oauth_clients_client_id'), 'oauth_clients', ['client_id'], unique=True)

    op.create_table(
        'oauth_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=48), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('redirect_uri', sa.Text(), nullable=False),
        sa.Column('code_challenge', sa.String(length=128), nullable=False),
        sa.Column('scope', sa.String(length=128), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_oauth_codes_code'), 'oauth_codes', ['code'], unique=True)
    op.create_index(op.f('ix_oauth_codes_client_id'), 'oauth_codes', ['client_id'], unique=False)

    op.create_table(
        'oauth_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=48), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=128), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_oauth_tokens_token'), 'oauth_tokens', ['token'], unique=True)
    op.create_index(op.f('ix_oauth_tokens_user_id'), 'oauth_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_oauth_tokens_user_id'), table_name='oauth_tokens')
    op.drop_index(op.f('ix_oauth_tokens_token'), table_name='oauth_tokens')
    op.drop_table('oauth_tokens')
    op.drop_index(op.f('ix_oauth_codes_client_id'), table_name='oauth_codes')
    op.drop_index(op.f('ix_oauth_codes_code'), table_name='oauth_codes')
    op.drop_table('oauth_codes')
    op.drop_index(op.f('ix_oauth_clients_client_id'), table_name='oauth_clients')
    op.drop_table('oauth_clients')
