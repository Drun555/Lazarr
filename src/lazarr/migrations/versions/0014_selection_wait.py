"""Temporarily hide selection prompts without pausing searches."""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "subtasks", sa.Column("selection_hidden_until", sa.Float(), nullable=False, server_default="0")
    )


def downgrade():
    op.drop_column("subtasks", "selection_hidden_until")
