"""Consolidate media tasks without discarding episode versions or decisions."""

import time
from copy import deepcopy
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def remap(value, ids):
    if isinstance(value, dict):
        return {
            key: ids.get(item, item) if key == "subtask_id" else remap(item, ids)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [remap(item, ids) for item in value]
    return value


def upgrade():
    db = op.get_bind()
    meta = sa.MetaData()
    meta.reflect(db)
    tasks, seasons, subs = (meta.tables[n] for n in ("tasks", "task_seasons", "subtasks"))
    links, decisions = (meta.tables[n] for n in ("subtask_assets", "candidate_decisions"))
    downloads, settings, audits = (meta.tables[n] for n in ("downloads", "settings", "audit_events"))
    groups = {}
    for row in db.execute(sa.select(tasks).order_by(tasks.c.updated_at.desc(), tasks.c.id.desc())).mappings():
        groups.setdefault(row["media_id"], []).append(dict(row))
    task_ids, sub_ids = {}, {}
    for media_id, rows in groups.items():
        if len(rows) < 2:
            continue
        keeper = rows[0]["id"]
        db.execute(
            audits.insert().values(
                user_id=None,
                action="task.merge",
                target=str(keeper),
                details={"media_id": media_id, "previous_tasks": rows},
                created_at=time.time(),
            )
        )
        # An active season remains active when another former task was paused.
        db.execute(tasks.update().where(tasks.c.id == keeper).values(paused=all(r["paused"] for r in rows)))
        for old in rows[1:]:
            task_ids[old["id"]] = keeper
            for membership in (
                db.execute(sa.select(seasons).where(seasons.c.task_id == old["id"])).mappings().all()
            ):
                same = (
                    db.execute(
                        sa.select(seasons).where(
                            seasons.c.task_id == keeper,
                            seasons.c.selection_key == membership["selection_key"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if same:
                    db.execute(
                        seasons.update()
                        .where(seasons.c.id == same["id"])
                        .values(whole_season=same["whole_season"] or membership["whole_season"])
                    )
                    db.execute(seasons.delete().where(seasons.c.id == membership["id"]))
                else:
                    db.execute(
                        seasons.update().where(seasons.c.id == membership["id"]).values(task_id=keeper)
                    )
            for sub in db.execute(sa.select(subs).where(subs.c.task_id == old["id"])).mappings().all():
                if old["requirements"] != rows[0]["requirements"]:
                    # Keep versions, but re-evaluate them against the unified requirements.
                    db.execute(
                        links.update()
                        .where(links.c.subtask_id == sub["id"])
                        .values(current=False, pending=False, override=False)
                    )
                    db.execute(
                        subs.update()
                        .where(subs.c.id == sub["id"])
                        .values(status="queued", next_search_at=0, lease_until=0)
                    )
                same = (
                    db.execute(
                        sa.select(subs).where(subs.c.task_id == keeper, subs.c.part_key == sub["part_key"])
                    )
                    .mappings()
                    .first()
                )
                if not same:
                    db.execute(subs.update().where(subs.c.id == sub["id"]).values(task_id=keeper))
                    continue
                target = same["id"]
                sub_ids[sub["id"]] = target
                for table, unique in ((links, "asset_id"), (decisions, "release_id")):
                    for item in (
                        db.execute(sa.select(table).where(table.c.subtask_id == sub["id"])).mappings().all()
                    ):
                        existing = (
                            db.execute(
                                sa.select(table).where(
                                    table.c.subtask_id == target, table.c[unique] == item[unique]
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if existing:
                            db.execute(table.delete().where(table.c.id == item["id"]))
                        else:
                            values = {"subtask_id": target}
                            for field in ("report", "preflight", "verification"):
                                if field in item:
                                    values[field] = remap(item[field], sub_ids)
                            if table is links:
                                for flag in ("current", "pending"):
                                    occupied = db.execute(
                                        sa.select(links.c.id).where(
                                            links.c.subtask_id == target, links.c[flag].is_(True)
                                        )
                                    ).first()
                                    values[flag] = bool(item[flag] and not occupied)
                            db.execute(table.update().where(table.c.id == item["id"]).values(**values))
                db.execute(
                    subs.update()
                    .where(subs.c.id == target)
                    .values(
                        last_search_at=max(same["last_search_at"] or 0, sub["last_search_at"] or 0) or None,
                        next_search_at=0,
                        lease_until=0,
                    )
                )
                db.execute(subs.delete().where(subs.c.id == sub["id"]))
            db.execute(tasks.delete().where(tasks.c.id == old["id"]))
    if sub_ids:
        for table, fields in ((links, ("preflight", "verification")), (decisions, ("report",))):
            for row in db.execute(sa.select(table)).mappings().all():
                db.execute(
                    table.update()
                    .where(table.c.id == row["id"])
                    .values(**{field: remap(row[field], sub_ids) for field in fields})
                )
        for download in db.execute(sa.select(downloads)).mappings().all():
            plan = remap(download["plan"], sub_ids)
            bindings = {b["subtask_id"]: b for b in plan.get("bindings", [])}
            plan["bindings"] = list(bindings.values())
            stats = deepcopy(download["stats"])
            if stats.get("bindings"):
                stats["bindings"] = {
                    str(sub_ids.get(int(k), int(k))): v for k, v in stats["bindings"].items()
                }
            db.execute(
                downloads.update().where(downloads.c.id == download["id"]).values(plan=plan, stats=stats)
            )
    for entry in (
        db.execute(sa.select(settings).where(settings.c.key.like("search.request.%"))).mappings().all()
    ):
        value = entry["value"]
        if value.get("task_id") in task_ids:
            db.execute(
                settings.update()
                .where(settings.c.key == entry["key"])
                .values(value={**value, "task_id": task_ids[value["task_id"]]})
            )
    op.create_index("uq_tasks_media_id", "tasks", ["media_id"], unique=True)


def downgrade():
    # Merging is intentionally not reversed: references and files remain attached.
    op.drop_index("uq_tasks_media_id", table_name="tasks")
