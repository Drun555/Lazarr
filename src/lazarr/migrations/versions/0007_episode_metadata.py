"""Store episode descriptions and still images supplied by metadata providers."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("episodes", sa.Column("overview", sa.Text(), nullable=False, server_default=""))
    op.add_column("episodes", sa.Column("still", sa.Text(), nullable=True))
    # Existing rows were created before these fields existed. Mark their season
    # for one metadata refresh the next time it is opened.
    op.execute("UPDATE seasons SET refreshed_at = 0")


def downgrade():
    op.drop_column("episodes", "still")
    op.drop_column("episodes", "overview")
