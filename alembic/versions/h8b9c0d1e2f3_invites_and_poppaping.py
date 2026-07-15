"""member invite slots + PoppaPing monitoring integration

Revision ID: h8b9c0d1e2f3
Revises: g7a8b9c0d1e2
Create Date: 2026-07-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'g7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('max_invites', sa.Integer(), nullable=False, server_default='2'))
    op.add_column('users', sa.Column('poppaping_api_key', sa.String(length=100), nullable=True))
    op.add_column('instances', sa.Column('poppaping_monitor_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('instances', 'poppaping_monitor_id')
    op.drop_column('users', 'poppaping_api_key')
    op.drop_column('users', 'max_invites')
