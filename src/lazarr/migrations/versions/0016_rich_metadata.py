"""Keep season and episode metadata for portable library exports."""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("seasons", "episodes"):
        op.add_column(table, sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"))
    op.execute("UPDATE seasons SET refreshed_at = 0")


def downgrade():
    for table in ("episodes", "seasons"):
        op.drop_column(table, "metadata_json")
