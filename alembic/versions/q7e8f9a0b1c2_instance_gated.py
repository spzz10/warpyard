"""instances.gated — require Warpyard login to reach a server's web ingress

Revision ID: q7e8f9a0b1c2
Revises: p6d7e8f9a0b1
Create Date: 2026-07-15 15:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "q7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "p6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("gated", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("instances", "gated")
