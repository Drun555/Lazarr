import httpx
from lazarr.bundled.tmdb import Plugin
from lazarr.sdk import ProviderContext, MetadataItem, EpisodeInfo, SeasonInfo
from lazarr.services import CreateTask
from lazarr.models import Episode, Subtask
from sqlalchemy import select


async def test_tmdb_uses_explicit_season_group_and_preserves_canonical_numbers():
    def transport(request):
        if "/episode_group/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "groups": [
                        {
                            "name": "Season 2",
                            "episodes": [
                                {"season_number": 1, "episode_number": 39, "order": 13},
                                {"season_number": 1, "episode_number": 40, "order": 14},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 65942,
                "name": "Re:Zero",
                "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 85}],
                "episode_groups": {
                    "results": [{"id": "group", "type": 6, "name": "Seasons", "episode_count": 85}]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p = Plugin(ProviderContext({"api_key": "test-token"}, {}, client))
        item = await p.get_media("tv", "65942")
        assert item.seasons[0]["episode_count"] == 85
        assert item.episode_numbering["1:39"] == [
            {"season": 2, "episode": 14, "source": "tmdb:episode_group:group"}
        ]


def numbered_media():
    return MetadataItem(
        id="65942",
        kind="tv",
        title="Re:Zero",
        episode_numbering={
            f"1:{n}": [{"season": 2, "episode": n - 25, "source": "tmdb:episode_group:test"}]
            for n in range(26, 51)
        },
    )


def canonical_season():
    return SeasonInfo(
        number=1,
        title="Canonical",
        episodes=[EpisodeInfo(id=str(n), number=n, title=f"Episode {n}") for n in range(1, 67)],
    )


def test_part_selection_uses_shared_canonical_episodes(core):
    _, db, _, service = core
    media = numbered_media()
    season = canonical_season()
    first = service.create_from_metadata(
        CreateTask(media_id=media.id, kind="tv", season=2, numbering_season=2, episodes=list(range(14, 26))),
        media,
        season,
        1,
    )
    second = service.create_from_metadata(
        CreateTask(media_id=media.id, kind="tv", season=1, episodes=list(range(39, 51))), media, season, 2
    )
    tasks = {t["id"]: t for t in service.list_tasks()}
    assert first == second
    assert {s["canonical_season"] for s in tasks[first]["seasons"]} == {1}
    assert [s["episode"] for s in tasks[first]["subtasks"]] == list(range(14, 26))
    assert [s["canonical_episode"] for s in tasks[first]["subtasks"]] == list(range(39, 51))
    with db.session() as s:
        a = set(s.scalars(select(Subtask.episode_id).where(Subtask.task_id == first)))
        b = set(s.scalars(select(Subtask.episode_id).where(Subtask.task_id == second)))
        assert a == b and len(a) == 12


async def test_whole_alternate_season_refresh_does_not_add_next_season(core, monkeypatch):
    _, db, manager, service = core
    media = numbered_media()
    season = canonical_season()
    identity = service.create_from_metadata(
        CreateTask(media_id=media.id, kind="tv", season=2, numbering_season=2), media, season, 1
    )
    assert len(service.list_tasks()[0]["subtasks"]) == 25
    from lazarr.models import Season

    with db.session() as session:
        session.scalar(select(Season)).refreshed_at = 0

    async def get_media(self, *args):
        return media

    async def get_season(self, *args):
        return season

    monkeypatch.setattr(manager.classes["tmdb"], "get_media", get_media)
    monkeypatch.setattr(manager.classes["tmdb"], "get_season", get_season)
    await service.refresh_seasons()
    with db.session() as session:
        numbers = set(
            session.scalars(
                select(Episode.number)
                .join(Subtask, Subtask.episode_id == Episode.id)
                .where(Subtask.task_id == identity)
            )
        )
    assert numbers == set(range(26, 51))
