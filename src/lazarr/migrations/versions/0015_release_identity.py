"""One tracker topic is one release; downloads retain immutable torrent snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("releases", sa.Column("files", sa.JSON(), nullable=False, server_default="[]"))
    db = op.get_bind()
    meta = sa.MetaData()
    meta.reflect(db)
    releases, downloads, decisions, settings = (
        meta.tables[name] for name in ("releases", "downloads", "candidate_decisions", "settings")
    )
    groups = {}
    for row in db.execute(sa.select(releases).order_by(releases.c.id)).mappings():
        groups.setdefault((row["provider"], row["external_id"]), []).append(dict(row))
    remap = {}
    for rows in groups.values():
        keeper, latest = rows[0], rows[-1]
        ids = [row["id"] for row in rows]
        snapshot = db.execute(
            sa.select(downloads.c.plan).where(downloads.c.infohash == latest["revision"])
        ).scalar()
        for identity in ids:
            remap[identity] = keeper["id"]
        by_subtask = {}
        for decision in db.execute(
            sa.select(decisions).where(decisions.c.release_id.in_(ids)).order_by(decisions.c.id)
        ).mappings():
            by_subtask.setdefault(decision["subtask_id"], []).append(dict(decision))
        for choices in by_subtask.values():
            target = choices[0]
            report = max(choices, key=lambda row: (row["release_id"], row["updated_at"]))
            action = max(choices, key=lambda row: row["updated_at"])["action"]
            if len(choices) > 1:
                db.execute(decisions.delete().where(decisions.c.id.in_([row["id"] for row in choices[1:]])))
            db.execute(
                decisions.update()
                .where(decisions.c.id == target["id"])
                .values(
                    release_id=keeper["id"],
                    report=report["report"],
                    action=action,
                    updated_at=max(row["updated_at"] for row in choices),
                )
            )
        db.execute(downloads.update().where(downloads.c.release_id.in_(ids)).values(release_id=keeper["id"]))
        if len(ids) > 1:
            db.execute(releases.delete().where(releases.c.id.in_(ids[1:])))
        db.execute(
            releases.update()
            .where(releases.c.id == keeper["id"])
            .values(revision=latest["revision"], data=latest["data"], files=(snapshot or {}).get("files", []))
        )
    for row in (
        db.execute(sa.select(settings).where(settings.c.key.like("season_mapping.%"))).mappings().all()
    ):
        value = dict(row["value"])
        value["releases"] = sorted({remap.get(identity, identity) for identity in value.get("releases", [])})
        db.execute(settings.update().where(settings.c.key == row["key"]).values(value=value))
    op.create_index("uq_releases_source", "releases", ["provider", "external_id"], unique=True)


def downgrade():
    op.drop_index("uq_releases_source", table_name="releases")
    op.drop_column("releases", "files")
