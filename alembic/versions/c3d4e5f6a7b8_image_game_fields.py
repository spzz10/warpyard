"""images: category + lgsm_game + ports + guidance (game servers)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-11 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('images', sa.Column('category', sa.String(length=16), nullable=False, server_default='os'))
    op.add_column('images', sa.Column('lgsm_game', sa.String(length=32), nullable=False, server_default=''))
    op.add_column('images', sa.Column('ports', sa.String(length=128), nullable=False, server_default=''))
    op.add_column('images', sa.Column('guidance', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('images', 'guidance')
    op.drop_column('images', 'ports')
    op.drop_column('images', 'lgsm_game')
    op.drop_column('images', 'category')
