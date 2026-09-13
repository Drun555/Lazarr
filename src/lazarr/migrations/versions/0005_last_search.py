"""Persist actual per-episode search attempts for the library."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subtasks", sa.Column("last_search_at", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("subtasks", "last_search_at")
