"""instances.tls_passthrough — VM terminates its own HTTPS, edge SNI-passes :443

Revision ID: m3a4b5c6d7e8
Revises: l2f3a4b5c6d7
Create Date: 2026-07-13 18:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "l2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("tls_passthrough", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("instances", "tls_passthrough")
