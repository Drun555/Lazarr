"""Allow one task to monitor multiple season selections."""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_seasons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("selection_key", sa.String(), nullable=False),
        sa.Column("whole_season", sa.Boolean(), nullable=False),
        sa.Column("numbering", sa.JSON(), nullable=False),
        sa.UniqueConstraint("task_id", "selection_key"),
    )
    op.execute("""
        INSERT INTO task_seasons (task_id, season_id, selection_key, whole_season, numbering)
        SELECT tasks.id, tasks.season_id,
               CASE WHEN json_extract(tasks.numbering, '$.season') IS NOT NULL
                    THEN 'alt:' || json_extract(tasks.numbering, '$.season')
                    ELSE CAST(seasons.number AS TEXT) END,
               tasks.whole_season, tasks.numbering
        FROM tasks JOIN seasons ON seasons.id = tasks.season_id
    """)


def downgrade():
    op.drop_table("task_seasons")
