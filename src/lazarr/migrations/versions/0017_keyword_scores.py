"""Replace mandatory phrases with weighted keyword preferences."""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def convert(values):
    values = dict(values)
    phrase = str(values.pop("keyword", "") or "").strip()
    values.setdefault("keywords", [{"text": phrase, "points": 50}] if phrase else [])
    return values


def upgrade():
    connection = op.get_bind()
    tasks = sa.table("tasks", sa.column("id", sa.Integer), sa.column("requirements", sa.JSON))
    for identity, requirements in connection.execute(sa.select(tasks.c.id, tasks.c.requirements)).all():
        connection.execute(
            tasks.update().where(tasks.c.id == identity).values(requirements=convert(requirements))
        )
    entries = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
    settings = connection.execute(
        sa.select(entries.c.value).where(entries.c.key == "app")
    ).scalar_one_or_none()
    if settings and "defaults" in settings:
        connection.execute(
            entries.update()
            .where(entries.c.key == "app")
            .values(
                value={
                    **settings,
                    "defaults": convert(settings["defaults"]),
                }
            )
        )


def downgrade():
    # JSON preferences have no schema changes and cannot be represented by one mandatory phrase.
    pass
