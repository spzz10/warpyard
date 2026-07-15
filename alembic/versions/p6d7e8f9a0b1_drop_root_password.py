"""drop instances.root_password — dead since the console-autologin redesign

The browser console auto-logs-in via serial getty and SSH is key-based, so no root
password is ever set or stored; the column has been NULL on every row since the
cipassword removal. Dropping it closes out the old "encrypt at rest" TODO for good.

Revision ID: p6d7e8f9a0b1
Revises: o5c6d7e8f9a0
Create Date: 2026-07-15 13:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "o5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("instances", "root_password")


def downgrade() -> None:
    op.add_column("instances", sa.Column("root_password", sa.String(length=64), nullable=True))
