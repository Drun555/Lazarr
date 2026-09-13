"""Preserve display numbering separately from canonical episode identities."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("numbering", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("tasks", "numbering")
