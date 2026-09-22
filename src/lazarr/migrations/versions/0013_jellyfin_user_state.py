"""Persist Jellyfin favorites, ratings, playback history and video playlists."""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("playback_progress") as batch:
        batch.add_column(sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("likes", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("rating", sa.Float(), nullable=True))
    op.create_table(
        "playback_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("part_key", sa.String(80), nullable=False),
        sa.Column("position_ticks", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("failed", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("stopped_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("user_id", "item_id", "session_key"),
    )
    op.create_index("ix_playback_sessions_item_id", "playback_sessions", ["item_id"])
    op.create_table(
        "video_playlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("shares", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table("video_playlists")
    op.drop_table("playback_sessions")
    with op.batch_alter_table("playback_progress") as batch:
        batch.drop_column("rating")
        batch.drop_column("likes")
        batch.drop_column("is_favorite")
