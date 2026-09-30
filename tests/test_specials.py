from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from lazarr.library_metadata import sidecars
from lazarr.specials import infer_position


CATALOG = [
    {
        "number": 1,
        "episodes": [{"number": 1, "air_date": "2020-01-01"}, {"number": 2, "air_date": "2020-01-08"}],
    },
    {"number": 2, "episodes": [{"number": 1, "air_date": "2021-01-01"}]},
]


@pytest.mark.parametrize(
    ("aired", "expected"),
    [
        ("2019-12-01", {"airsbefore_season": 1}),
        ("2020-01-04", {"airsbefore_season": 1, "airsbefore_episode": 2}),
        ("2020-06-01", {"airsafter_season": 1}),
        ("2022-01-01", {"airsafter_season": 2}),
        ("2020-01-01", {}),
        (None, {}),
        ("invalid", {}),
    ],
)
def test_date_placement(aired, expected):
    assert infer_position(aired, CATALOG) == expected


def test_special_nfo_and_regular_episode():
    media = SimpleNamespace(
        kind="tv", title="Show", year=2020, provider="tmdb", external_id="1", metadata_json={}
    )
    ep = SimpleNamespace(title="Special", overview="", air_date="2020-01-04", external_id="2", still=None)
    for season_number in (0, 1):
        entries = sidecars(
            media,
            ep,
            SimpleNamespace(number=season_number, title=""),
            {"season": season_number, "episode": 1},
            Path("/library/Season"),
            "episode",
            Path("/library"),
            {"airsbefore_season": 1, "airsbefore_episode": 2},
        )
        node = ET.fromstring(next(e["content"] for e in entries if e["path"].endswith("/episode.nfo")))
        assert node.findtext("airsbefore_season") == ("1" if season_number == 0 else None)
        assert node.findtext("airsbefore_episode") == ("2" if season_number == 0 else None)
        assert node.find("airsafter_season") is None
