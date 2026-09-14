from sqlalchemy import select, func
import pytest
from lazarr.config import Requirements
from lazarr.models import Media, Task, Subtask, Season, Episode, User
from lazarr.sdk import MetadataItem, SeasonInfo, EpisodeInfo
from lazarr.security import change_account, permitted
from lazarr.services import CreateTask


def test_tasks_share_media_but_not_requirements(core, media, season):
    _, db, _, service = core
    first = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, requirements=Requirements(audio_languages=["ru"])),
        media,
        season,
        1,
    )
    second = service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[2],
            requirements=Requirements(audio_languages=["ja"], max_resolution=2160),
        ),
        media,
        season,
        2,
    )
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Media)) == 1
        assert session.get(Task, first).media_id == session.get(Task, second).media_id
        assert session.get(Task, first).requirements["audio_languages"] == ["ru"]
        assert session.get(Task, second).requirements["audio_languages"] == ["ja"]
        assert len(list(session.scalars(select(Subtask).where(Subtask.task_id == first)))) == 3
        assert len(list(session.scalars(select(Subtask).where(Subtask.task_id == second)))) == 1
    settings = service.settings()
    settings.defaults.audio_languages = ["en"]
    service.set_settings(settings, 1)
    with db.session() as session:
        assert session.get(Task, first).requirements["audio_languages"] == ["ru"]


def test_movie_has_one_subtask_and_kind_namespaces_ids(core):
    _, db, _, service = core
    media = MetadataItem(id="42", kind="movie", title="A Movie")
    task = service.create_from_metadata(CreateTask(media_id="42", kind="movie"), media, None, 1)
    with db.session() as session:
        parts = list(session.scalars(select(Subtask).where(Subtask.task_id == task)))
        assert len(parts) == 1 and parts[0].episode_id is None and parts[0].part_key == "movie"


def test_invalid_episode_selection_is_atomic(core, media, season):
    _, db, _, service = core
    with pytest.raises(ValueError):
        service.create_from_metadata(
            CreateTask(media_id="42", kind="tv", season=1, episodes=[99]), media, season, 1
        )
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(Media)) == 0


def test_last_admin_cannot_be_disabled(core):
    _, db, _, _ = core
    with db.session() as session:
        change_account(session, session.get(User, 2), active=False)
    with db.session() as session:
        with pytest.raises(ValueError, match="последнего"):
            change_account(session, session.get(User, 1), active=False)
        assert permitted(session.get(User, 1), "providers")
        session.get(User, 2).role = "user"
        session.get(User, 2).active = True
        assert not permitted(session.get(User, 2), "providers")
        assert permitted(session.get(User, 2), "tasks")


async def test_refresh_adds_episodes_only_to_whole_season(core, media, season, monkeypatch):
    _, db, plugins, service = core
    first = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    second = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 2
    )

    async def get_season(self, media_id, number):
        return SeasonInfo(number=1, episodes=season.episodes + [EpisodeInfo(id="4", number=4, title="New")])

    monkeypatch.setattr(plugins.classes["tmdb"], "get_season", get_season)
    with db.session() as session:
        session.scalar(select(Season)).refreshed_at = 0
    await service.refresh_seasons()
    with db.session() as session:
        assert len(list(session.scalars(select(Subtask).where(Subtask.task_id == first)))) == 4
        assert len(list(session.scalars(select(Subtask).where(Subtask.task_id == second)))) == 1
        assert len(list(session.scalars(select(Episode)))) == 4


def test_settings_validate_timezone_and_container_paths(monkeypatch):
    from lazarr.config import Settings

    monkeypatch.setenv("LAZARR_MOVIE_PATH", "/downloads/movies")
    monkeypatch.setenv("LAZARR_SERIES_PATH", "/downloads/series")
    assert Settings().movie_path == "/downloads/movies"
    assert Settings().series_path == "/downloads/series"
    assert Settings().prefer_full_subtitles is True
    monkeypatch.setenv("TZ", "Europe/Saratov")
    assert str(Settings().timezone) == "Europe/Saratov"
    assert "timezone" not in Settings().model_dump()
    assert Settings(window_start="06:30", timezone="UTC").search_start == "06:30"
