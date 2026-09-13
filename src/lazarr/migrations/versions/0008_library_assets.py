"""Retain verified library files independently from their originating tasks."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "library_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id"), nullable=False),
        sa.Column("episode_id", sa.Integer(), sa.ForeignKey("episodes.id"), nullable=True),
        sa.Column("part_key", sa.String(length=80), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("media_assets.id"), nullable=False),
        sa.Column("preflight", sa.JSON(), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("media_id", "part_key", "asset_id"),
    )
    # Every current SubtaskAsset was promoted only after complete verification.
    # Copy it before tasks can be deleted so existing libraries keep working.
    op.execute(
        """
        INSERT OR IGNORE INTO library_assets
            (media_id, episode_id, part_key, asset_id, preflight, verification, created_at)
        SELECT tasks.media_id,
               subtasks.episode_id,
               CASE WHEN subtasks.episode_id IS NULL
                    THEN 'movie'
                    ELSE 'episode:' || subtasks.episode_id END,
               subtask_assets.asset_id,
               subtask_assets.preflight,
               subtask_assets.verification,
               CAST(strftime('%s', 'now') AS REAL)
        FROM subtask_assets
        JOIN subtasks ON subtasks.id = subtask_assets.subtask_id
        JOIN tasks ON tasks.id = subtasks.task_id
        WHERE subtask_assets.current = 1
        """
    )


def downgrade():
    op.drop_table("library_assets")
