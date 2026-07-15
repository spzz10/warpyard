"""instances.encrypted — disk on the ZFS-encrypted-at-rest pool (opt-in)

Revision ID: n4b5c6d7e8f9
Revises: m3a4b5c6d7e8
Create Date: 2026-07-13 19:25:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "m3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("instances", "encrypted")
