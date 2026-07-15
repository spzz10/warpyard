"""instances: backups_enabled + last_backup_at (PBS backups add-on)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-12 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instances', sa.Column('backups_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('instances', sa.Column('last_backup_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('instances', 'last_backup_at')
    op.drop_column('instances', 'backups_enabled')
