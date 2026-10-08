import pytest

from lazarr import search_runtime as runtime
from lazarr.matcher import episode_numbers


@pytest.fixture(autouse=True)
def restore_generation(monkeypatch):
    monkeypatch.setattr(runtime, "_generation", None)


def test_engine_identity_is_stable_across_startups():
    manager = runtime.SearchEngineManager()
    manager.bootstrap()
    before = manager.status()
    runtime._generation = None
    runtime.SearchEngineManager().bootstrap()
    assert manager.status() == before
    assert before["source"] == "application"
    assert len(before["identity"].split(":")[1]) == 64
    assert episode_numbers("Show.S01E02.mkv") == (1, {2}, False)


def test_search_queries_supports_old_engine(media, monkeypatch):
    from types import SimpleNamespace
    from lazarr import search_runtime
    from lazarr.provider_utils import search_queries

    monkeypatch.setattr(
        search_runtime,
        "_generation",
        SimpleNamespace(provider_utils=SimpleNamespace(search_titles=lambda media: [media.title])),
    )
    assert search_queries(media, season=1, year=2022) == ["Example Show 2022", "Example Show"]
