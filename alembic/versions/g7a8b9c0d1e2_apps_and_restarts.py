"""images.blurb (app library taglines) + instances scheduled-restart fields

Revision ID: g7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-13 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('images', sa.Column('blurb', sa.String(length=120), nullable=True))
    op.add_column('instances', sa.Column('restart_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('instances', sa.Column('restart_at', sa.String(length=5), nullable=True))
    op.add_column('instances', sa.Column('last_auto_restart_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('instances', 'last_auto_restart_at')
    op.drop_column('instances', 'restart_at')
    op.drop_column('instances', 'restart_enabled')
    op.drop_column('images', 'blurb')
