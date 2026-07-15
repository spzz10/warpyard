"""share board: instances.shared/shared_note + users.share_by_default

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-12 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('share_by_default', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('instances', sa.Column('shared', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('instances', sa.Column('shared_note', sa.String(length=140), nullable=True))


def downgrade() -> None:
    op.drop_column('instances', 'shared_note')
    op.drop_column('instances', 'shared')
    op.drop_column('users', 'share_by_default')
