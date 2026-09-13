"""Track Jellyfin playback progress independently for every user."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "playback_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("position_ticks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("played", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_played_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("user_id", "item_id"),
    )


def downgrade():
    op.drop_table("playback_progress")
