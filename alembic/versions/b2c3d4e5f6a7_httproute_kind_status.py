"""http_routes: kind + status (custom domains)

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5f6
Create Date: 2026-07-11 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1f2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # existing routes are all system + active; server_default backfills them
    op.add_column('http_routes', sa.Column('kind', sa.String(length=10), nullable=False, server_default='system'))
    op.add_column('http_routes', sa.Column('status', sa.String(length=10), nullable=False, server_default='active'))


def downgrade() -> None:
    op.drop_column('http_routes', 'status')
    op.drop_column('http_routes', 'kind')
