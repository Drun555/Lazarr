from lazarr.matcher import Matcher, episode_numbers
from lazarr.sdk import TorrentFile, MatchResult, Evidence
from conftest import candidate, request, audio_claim


def files(*paths):
    return [TorrentFile(index=i, path=p, size=1000, offset=i * 1000) for i, p in enumerate(paths)]


def test_batch_can_match_only_some_subtasks(media):
    paths = files("Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv")
    c = candidate(evidence=[audio_claim(paths[0].path), audio_claim(paths[1].path, ["en"])])
    report = Matcher().evaluate(c, [request(media, 1, 1), request(media, 2, 2)], paths, "abc")
    assert [r.result for r in report.evaluations] == [MatchResult.MATCH, MatchResult.MISMATCH]
    assert [b.subtask_id for b in report.plan.bindings] == [1]


def test_external_tracks_match_episode_not_just_language(media):
    paths = files(
        "Show.S01E01.1080p.mkv",
        "Show.S01E02.1080p.mkv",
        "Audio/RUS/Show.S01E02.mka",
        "Subs/RUS/Show.S01E01.ass",
    )
    result = Matcher().evaluate(
        candidate(), [request(media, 1, 1, subtitle_languages=["ru"]), request(media, 2, 2)], paths
    )
    first, second = result.evaluations
    assert first.result == MatchResult.UNKNOWN
    assert second.result == MatchResult.MATCH
    assert second.binding.tracks[0].file_index == 2
    assert [t.file_index for t in first.binding.tracks] == [3]


def test_all_episode_subtitles_are_bound_and_title_assigns_unknown_language(media):
    paths = files(
        "Nathan For You s01/Nathan For You - s01e01.mkv",
        "Nathan For You s01/Nathan For You - s01e01.srt",
        "Nathan For You s01/Subs/EN/Nathan For You - s01e01.ass",
    )
    item = candidate(
        title="Nathan For You / Сезон: 01 / Серии: 1-8 + rus Sub",
        evidence=[audio_claim(paths[0].path)],
    )
    result = (
        Matcher()
        .evaluate(
            item,
            [request(media, subtitle_languages=["ru"])],
            paths,
        )
        .evaluations[0]
    )
    subtitles = [track for track in result.binding.tracks if track.kind == "subtitle"]
    assert [(track.file_index, track.language) for track in subtitles] == [(1, "ru"), (2, "en")]
    assert subtitles[0].language_source == "title"
    assert result.binding.missing_subtitle_languages == []


def test_subtitle_path_marks_forced_and_size_identifies_full_track(media):
    paths = [
        TorrentFile(index=0, path="Show/Show.S01E01.1080p.mkv", size=1_000_000, offset=0),
        TorrentFile(
            index=1,
            path="Show/RUS Subs/Crunchyroll/Show.S01E01.ass",
            size=36_000,
            offset=1_000_000,
        ),
        TorrentFile(
            index=2,
            path="Show/RUS Subs/Crunchyroll/Надписи/Show.S01E01.ass",
            size=4_000,
            offset=1_036_000,
        ),
    ]
    result = (
        Matcher()
        .evaluate(
            candidate(evidence=[audio_claim(paths[0].path)]),
            [request(media, subtitle_languages=["ru"])],
            paths,
        )
        .evaluations[0]
    )

    subtitles = {track.file_index: track for track in result.binding.tracks if track.kind == "subtitle"}
    assert subtitles[2].forced is True
    assert subtitles[2].title == "Форсированные"
    assert subtitles[1].forced is False
    assert subtitles[1].title == "Полные"


def test_unknown_unrequested_subtitle_is_still_bound(media):
    paths = files("Show.S01E01.1080p.mkv", "Show.S01E01.srt")
    result = (
        Matcher()
        .evaluate(candidate(evidence=[audio_claim(paths[0].path)]), [request(media)], paths)
        .evaluations[0]
    )
    subtitle = next(track for track in result.binding.tracks if track.kind == "subtitle")
    assert subtitle.file_index == 1 and subtitle.language == "und"


def test_missing_subtitles_are_nonblocking(media):
    path = files("Show.S01E01.1080p.mkv")
    result = Matcher().evaluate(
        candidate(evidence=[audio_claim(path[0].path)]),
        [request(media, subtitle_languages=["ru", "en"])],
        path,
    )
    assert result.evaluations[0].result == MatchResult.MATCH
    assert result.plan.bindings[0].missing_subtitle_languages == ["en", "ru"]


def test_unknown_languages_do_not_mean_absent(media):
    path = files("Show.S01E01.1080p.mkv")
    result = Matcher().evaluate(candidate(), [request(media)], path)
    assert result.evaluations[0].result == MatchResult.UNKNOWN
    assert result.plan is None


def test_provider_batch_claim_not_assumed_for_all_files(media):
    paths = files("Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv")
    c = candidate(evidence=[Evidence(field="audio_languages", value=["ru"], source="description")])
    assert Matcher().evaluate(c, [request(media)], paths).evaluations[0].result == MatchResult.UNKNOWN


def test_actual_file_resolution_beats_release_title(media):
    paths = files("Show.S01E01.480p.mkv")
    c = candidate(evidence=[audio_claim(paths[0].path)])
    assert Matcher().evaluate(c, [request(media)], paths).evaluations[0].result == MatchResult.MISMATCH


def test_ambiguous_video_and_audio_require_selection(media):
    paths = files(
        "VersionA/Show.S01E01.1080p.mkv", "VersionB/Show.S01E01.1080p.mkv", "Audio/Show.S01E01.ru.mka"
    )
    result = Matcher().evaluate(candidate(), [request(media)], paths)
    assert result.evaluations[0].binding is None
    assert result.plan is None


def test_absolute_anime_numbers_need_explicit_mapping(media):
    paths = files("[Group] Example Show - 013 [1080p].mkv")
    c = candidate(evidence=[audio_claim(paths[0].path)])
    req = request(media)
    assert Matcher().evaluate(c, [req], paths).plan is None
    req.absolute_number = 13
    assert Matcher().evaluate(c, [req], paths).plan is not None


def test_multi_episode_range(media):
    paths = files("Show.S01E01-E03.1080p.mkv")
    c = candidate(evidence=[audio_claim(paths[0].path)])
    report = Matcher().evaluate(c, [request(media, 1, 1), request(media, 2, 3)], paths)
    assert len(report.plan.bindings) == 2
    assert {b.video_index for b in report.plan.bindings} == {0}


def test_identity_conflict_and_keyword(media):
    paths = files("Show.S01E01.1080p.mkv")
    c = candidate(external_ids={"imdb": "tt99"}, evidence=[audio_claim(paths[0].path)])
    assert Matcher().evaluate(c, [request(media)], paths).plan is None
    c = candidate(evidence=[audio_claim(paths[0].path)])
    assert Matcher().evaluate(c, [request(media, keyword="dub studio")], paths).plan is None


def test_resolution_below_maximum_is_acceptable(media):
    paths = files("Show.S01E01.1080p.mkv")
    c = candidate(evidence=[audio_claim(paths[0].path)])
    req = request(media, max_resolution=2160)
    assert Matcher().evaluate(c, [req], paths).evaluations[0].result == MatchResult.MATCH


def test_archives_are_not_video(media):
    assert Matcher().evaluate(candidate(), [request(media)], files("Show.S01E01.rar")).plan is None


def test_episode_parser():
    assert episode_numbers("Season 2/Show E03.mkv") == (2, {3}, False)
    assert episode_numbers("Show.2x03.mkv") == (2, {3}, False)
    assert episode_numbers("Show.S01E03-E01.mkv") == (None, set(), False)
    assert episode_numbers("Show [TV-3]/Show S3 - 01 (1080p).mkv") == (3, {1}, False)
    assert episode_numbers("Show TV-3 - 01.mkv") == (3, {1}, False)
    assert episode_numbers("Show S3 - 1.mkv") == (3, {1}, False)
    assert episode_numbers("Show III/Show - 01.mkv", season_hint=3) == (3, {1}, False)
    assert episode_numbers("Show III/Show - 1.mkv", season_hint=3) == (None, {1}, True)
    assert episode_numbers("Show III/Show - 101.mkv", season_hint=3) == (None, {101}, True)


def test_release_season_fills_two_digit_episode_filename(media):
    paths = files("[Group] Example Show III - 01.mkv", "[Group] Example Show III - 02.mkv")
    item = candidate(
        title="Example Show (TV-3) [01-02] 1080p",
        evidence=[audio_claim(path.path) for path in paths],
    )
    requests = [request(media, 1, 1), request(media, 2, 2)]
    for subtask in requests:
        subtask.season = 3
    report = Matcher().evaluate(item, requests, paths)
    assert [result.binding.video_index for result in report.evaluations] == [0, 1]


def test_longer_title_and_sequel_are_not_same_identity(media):
    paths = files("Show.S01E01.1080p.mkv")
    for title in ["Another Example Show (2020)", "Example Show 2 (2020)"]:
        c = candidate(title=title, external_ids={}, evidence=[audio_claim(paths[0].path)])
        assert Matcher().evaluate(c, [request(media)], paths).plan is None


def test_explicit_wrong_episode_is_mismatch(media):
    paths = files("Show.S01E02.1080p.mkv")
    c = candidate(evidence=[audio_claim(paths[0].path)])
    report = Matcher().evaluate(c, [request(media, episode=1)], paths)
    assert report.evaluations[0].result == MatchResult.MISMATCH


def test_rezero_part_two_real_torrent_layout():
    import json
    from pathlib import Path
    from lazarr.sdk import MetadataItem, SubtaskRequest, Candidate
    from lazarr.config import Requirements

    paths = json.loads((Path(__file__).parent / "fixtures/rezero-files.json").read_text())
    torrent_files = files(*paths)
    media = MetadataItem(id="65942", kind="tv", title="Re:Zero", year=2016)
    requests = [
        SubtaskRequest(
            id=n,
            media=media,
            season=2,
            episode=n,
            requirements=Requirements(audio_languages=["ru"], subtitle_languages=["en", "ru"]),
        )
        for n in range(14, 26)
    ]
    candidate = Candidate(
        provider="rutracker",
        id="6088393",
        url="https://rutracker.org/forum/viewtopic.php?t=6088393",
        title="Re:Zero 2nd Season Part 2 [2021] [1080p]",
    )
    report = Matcher().evaluate(candidate, requests, torrent_files)
    assert len(report.evaluations) == 12
    for result in report.evaluations:
        assert next(c for c in result.criteria if c.field == "episode").result == MatchResult.MATCH
        assert f" - {result.subtask_id} [" in result.binding.video_path
        english = [t for t in result.binding.tracks if t.kind == "subtitle" and t.language == "en"]
        assert len(english) == 1 and f" - {result.subtask_id} [" in english[0].path
        assert result.result == MatchResult.UNKNOWN  # Audio language and title identity are not proven.
    assert report.plan is None
    assert episode_numbers("Show - 2nd Season - Part 2 - 01.mkv") == (2, {1}, False)
    assert episode_numbers("Show - 2nd Season - Part 2.mkv")[1] != {2}


def test_tv_release_year_is_not_mistaken_for_series_premiere(media):
    series = request(media)
    criterion = Matcher().identity(
        candidate(title="Example Show (TV-2) [2021] 1080p", external_ids={}), series
    )
    assert criterion.result == MatchResult.UNKNOWN
    movie = media.model_copy(update={"kind": "movie"})
    criterion = Matcher().identity(
        candidate(title="Example Show (2021) 1080p", external_ids={}), request(movie)
    )
    assert criterion.result == MatchResult.MISMATCH


def test_rezero_track_specs_bind_external_audio_to_each_episode():
    import json
    from pathlib import Path
    from lazarr.sdk import MetadataItem, SubtaskRequest
    from lazarr.config import Requirements
    from lazarr.provider_utils import description_evidence

    root = Path(__file__).parent / "fixtures"
    paths = json.loads((root / "rezero-files.json").read_text())
    item = candidate(evidence=description_evidence((root / "rezero-tracks.txt").read_text()))
    media = MetadataItem(id="65942", kind="tv", title="Re:Zero", year=2016)
    requests = [
        SubtaskRequest(
            id=n,
            media=media,
            season=2,
            episode=n,
            requirements=Requirements(
                audio_languages=["Русский", "jap"], subtitle_languages=["rus", "English"]
            ),
        )
        for n in range(14, 26)
    ]
    report = Matcher().evaluate(item, requests, files(*paths))
    for result in report.evaluations:
        assert next(c for c in result.criteria if c.field == "audio").result == MatchResult.MATCH
        assert next(c for c in result.criteria if c.field == "subtitles").result == MatchResult.MATCH
        tracks = result.binding.tracks
        assert any(t.kind == "audio" and t.language == "ja" and t.embedded for t in tracks)
        external = [t for t in tracks if t.kind == "audio" and not t.embedded]
        assert len(external) == 3
        assert all(t.language == "ru" and f" - {result.subtask_id} [" in t.path for t in external)
    # The description cannot manufacture an external track missing from the torrent.
    only_video = [p for p in paths if p.endswith(".mkv")]
    report = Matcher().evaluate(item, requests, files(*only_video))
    assert all(
        next(c for c in r.criteria if c.field == "audio").result == MatchResult.UNKNOWN
        for r in report.evaluations
    )


def test_multiple_external_languages_remain_ambiguous(media):
    from lazarr.provider_utils import description_evidence

    c = candidate(
        evidence=description_evidence("Аудио: RUS: AAC (внешним файлом)\nАудио: JAP: AAC (внешним файлом)")
    )
    result = Matcher().evaluate(c, [request(media)], files("Show.S01E01.mkv", "Show.S01E01.mka"))
    assert next(c for c in result.evaluations[0].criteria if c.field == "audio").result == MatchResult.UNKNOWN
