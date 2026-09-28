from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from lazarr.models import Release, Download, CandidateDecision, ConfigEntry
from lazarr.release_files import mapping_catalog, remap_binding
from lazarr.services import CreateTask
from test_season_mapping import files, seed_release, releases_key
from lazarr.models import Task


def test_migration_merges_topics_and_preserves_torrent_snapshots(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    old_id = seed_release(config, db)
    migration = Config()
    migration.set_main_option("script_location", str(Path(__file__).parents[1] / "src/lazarr/migrations"))
    migration.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(migration, "0014")
    with db.session() as session:
        session.execute(
            text(
                "INSERT INTO releases (id,provider,external_id,revision,data) VALUES (2,'nyaa','test','new-hash',:data)"
            ),
            {"data": '{"title":"Updated release"}'},
        )
        latest_files = [file.model_dump() for file in files("Episode2.mkv", "Pilot.mkv")]
        session.add(
            Download(
                infohash="new-hash",
                release_id=2,
                save_path="/tmp/new-hash",
                torrent_file="new.torrent",
                plan={"files": latest_files},
            )
        )
        session.add(
            CandidateDecision(
                subtask_id=1, release_id=2, report={"binding": {"video_index": 1}}, action="selected"
            )
        )
        entry = session.get(ConfigEntry, releases_key(session.get(Task, 1), 1))
        entry.value = {"releases": [old_id, 2]}
    db.migrate()
    with db.session() as session:
        release = session.scalar(select(Release))
        assert release.id == old_id and release.revision == "new-hash"
        assert release.data["title"] == "Updated release"
        assert release.files == latest_files
        assert len(list(session.scalars(select(Release)))) == 1
        downloads = list(session.scalars(select(Download).order_by(Download.id)))
        assert [download.infohash for download in downloads] == ["mapping-test", "new-hash"]
        assert {download.release_id for download in downloads} == {old_id}
        decisions = list(session.scalars(select(CandidateDecision)))
        assert len(decisions) == 1 and decisions[0].id == 1
        assert decisions[0].report["binding"]["video_index"] == 1
        assert session.get(ConfigEntry, releases_key(session.get(Task, 1), 1)).value["releases"] == [old_id]


def test_binding_remaps_indices_and_preserves_removed_or_changed_files():
    old = [file.model_dump() for file in files("Pilot.mkv", "Pilot.ru.srt", "Removed.mkv")]
    latest = [file.model_dump() for file in files("New.mkv", "Pilot.ru.srt", "Pilot.mkv")]
    binding = {"video_index": 0, "tracks": [{"file_index": 1, "path": "Pilot.ru.srt"}]}
    mapped = remap_binding(binding, old, latest)
    assert mapped["video_index"] == 2 and mapped["tracks"][0]["file_index"] == 1
    assert binding["video_index"] == 0
    latest[2]["size"] = 200
    assert remap_binding(binding, old, latest) is None
    catalog = mapping_catalog("new", latest, [("old", old)])
    assert len(catalog) == 5
    assert remap_binding(binding, old, catalog)["video_index"] == 3
    assert catalog[3]["revision"] == "old" and catalog[3]["source_index"] == 0
