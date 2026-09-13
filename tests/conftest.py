import pytest
from lazarr.config import RuntimeConfig
from lazarr.db import Database
from lazarr.security import SecretStore, password_hash
from lazarr.plugins import PluginManager
from lazarr.services import TaskService
from lazarr.models import User
from lazarr.sdk import MetadataItem, EpisodeInfo, SeasonInfo, Candidate, Evidence, SubtaskRequest
from lazarr.config import Requirements


@pytest.fixture
def core(tmp_path):
    config = RuntimeConfig(tmp_path / "data", background=False)
    db = Database(config.data_dir / "lazarr.sqlite")
    db.migrate()
    plugins = PluginManager(db, config, SecretStore(config.data_dir / "secret.key"))
    plugins.bootstrap()
    plugins.request_interval = 0  # Network spacing is verified with a fake clock in test_rate_limit.py.
    service = TaskService(db, plugins)
    service.settings()
    with db.session() as session:
        session.add_all(
            [
                User(username="alice", password_hash=password_hash("a-safe-password")),
                User(username="bob", password_hash=password_hash("b-safe-password")),
            ]
        )
    settings = service.settings()
    settings.movie_path = str(tmp_path / "downloads/movies")
    settings.series_path = str(tmp_path / "downloads/series")
    service.set_settings(settings, 1)
    yield config, db, plugins, service
    db.engine.dispose()


@pytest.fixture
def media():
    return MetadataItem(
        id="42",
        kind="tv",
        title="Example Show",
        original_title="Example Show",
        year=2020,
        external_ids={"tmdb": "42", "imdb": "tt0042"},
        seasons=[{"number": 1, "title": "Season 1", "episode_count": 3}],
    )


@pytest.fixture
def season():
    return SeasonInfo(
        number=1,
        title="Season 1",
        episodes=[
            EpisodeInfo(id=str(i), number=i, title=f"Episode {i}", air_date="2020-01-01") for i in [1, 2, 3]
        ],
    )


def candidate(**kwargs):
    values = dict(
        provider="nyaa",
        id="100",
        url="https://nyaa.si/view/100",
        title="Example Show (2020) 1080p",
        external_ids={"imdb": "tt0042"},
    )
    values.update(kwargs)
    return Candidate(**values)


def audio_claim(path, languages=None, complete=True):
    return Evidence(
        field="audio_languages",
        value=languages or ["ru"],
        source="structured",
        scope="file",
        file_path=path,
        complete=complete,
    )


def request(media, identity=1, episode=1, **requirements):
    return SubtaskRequest(
        id=identity, media=media, season=1, episode=episode, requirements=Requirements(**requirements)
    )
