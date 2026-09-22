import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.jellyfin import object_id
from lazarr.models import Download, Episode, LibraryAsset, MediaAsset
from lazarr.torrent import probe_file
from test_jellyfin import jellyfin_login, playable_episode


@pytest.fixture
def video(core, media, season):
    _, subtitle = playable_episode(core, media, season)
    path = subtitle.with_name("episode.mkv")
    sidecar = path.with_suffix(".srt")
    sidecar.write_text(
        "1\n00:00:00,200 --> 00:00:01,200\nHello world\n\n2\n00:00:01,300 --> 00:00:02,800\nSecond line\n",
        encoding="utf-8",
    )
    metadata = path.with_suffix(".ffmeta")
    metadata.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Intro\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=3000\ntitle=Main\n",
        encoding="utf-8",
    )
    attachment = path.with_suffix(".ttf")
    attachment.write_bytes(b"font attachment fixture")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10:d=3",
            "-i",
            str(sidecar),
            "-i",
            str(metadata),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-c:v",
            "mpeg4",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=eng",
            "-attach",
            str(attachment),
            "-metadata:s:t:0",
            "mimetype=application/x-truetype-font",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    with core[1].session() as db:
        asset = db.scalar(select(MediaAsset))
        asset.probe = probe_file(path)
        asset.resolution = 90
        episode_id = db.scalar(select(Episode.id).order_by(Episode.id))
    with TestClient(create_app(core[0])) as client:
        auth = jellyfin_login(client)
        yield client, object_id("episode", episode_id), path, auth


def test_embedded_subtitle_conversion_timestamps_hls_and_cache(video, core):
    client, identity, path, auth = video
    before = path.read_bytes()
    response = client.post(f"/Items/{identity}/PlaybackInfo", json={})
    assert response.status_code == 200, response.text
    source = response.json()["MediaSources"][0]
    stream = next(s for s in source["MediaStreams"] if s["Type"] == "Subtitle" and not s["IsExternal"])
    assert stream["SupportsExternalStream"] and stream["IsTextSubtitleStream"]
    url = stream["DeliveryUrl"].split("?")[0].replace(".srt", ".vtt")
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.text.startswith("WEBVTT") and "Hello world" in response.text
    shifted = client.get(url.replace("/0/Stream", "/10000000/Stream"), params={"endPositionTicks": 28000000})
    assert shifted.status_code == 200 and "00:00:00.300 --> 00:00:01.800" in shifted.text
    assert "Hello world" in shifted.text  # A cue crossing the seek point must survive.
    copied = client.get(
        url.replace("/0/Stream", "/10000000/Stream"),
        params={"copyTimestamps": "true", "addVttTimeMap": "true"},
    )
    assert "00:00:01.300 --> 00:00:02.800" in copied.text
    assert "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0" in copied.text
    playlist = client.get(
        f"/Videos/{identity}/{identity}/Subtitles/{stream['Index']}/subtitles.m3u8",
        params={"segmentLength": 1},
    )
    assert playlist.status_code == 200 and "#EXT-X-ENDLIST" in playlist.text
    urls = [line for line in playlist.text.splitlines() if line.startswith("/Videos")]
    assert len(urls) == 3
    client.headers.pop("X-Emby-Token")
    assert client.get(url).status_code == 401
    assert client.get(urls[1]).status_code == 200
    client.headers["X-Emby-Token"] = auth["AccessToken"]
    cached = list((core[0].data_dir / "cache/jellyfin-resources").glob("*.vtt"))
    mtimes = {p: p.stat().st_mtime_ns for p in cached}
    assert client.get(url).status_code == 200
    assert all(p.stat().st_mtime_ns == stamp for p, stamp in mtimes.items())
    assert client.get(url.replace(".vtt", ".exe")).status_code == 415
    assert client.get(url, params={"endPositionTicks": 0}).status_code == 400
    assert path.read_bytes() == before


def test_chapters_segments_trickplay_attachments_and_fonts(video, core):
    client, identity, path, _ = video
    item = client.get(f"/Items/{identity}").json()
    assert [c["Name"] for c in item["Chapters"]] == ["Intro", "Main"]
    assert item["Trickplay"][identity]["320"]["ThumbnailCount"] == 1
    segments = client.get(f"/MediaSegments/{identity}").json()
    assert segments["TotalRecordCount"] == 1
    assert segments["Items"][0]["Type"] == "Intro"
    assert segments["Items"][0]["EndTicks"] == 10000000
    assert (
        client.get(f"/MediaSegments/{identity}", params={"includeSegmentTypes": "Outro"}).json()["Items"]
        == []
    )
    assert client.get(f"/MediaSegments/{identity}").json() == segments
    playlist = client.get(f"/Videos/{identity}/Trickplay/320/tiles.m3u8")
    assert "#EXT-X-TILES:RESOLUTION=320x180,LAYOUT=5x5,DURATION=10.000" in playlist.text
    tile = client.get(f"/Videos/{identity}/Trickplay/320/0.jpg")
    assert tile.status_code == 200, tile.text if tile.status_code != 200 else ""
    from PIL import Image
    import io

    assert Image.open(io.BytesIO(tile.content)).size == (1600, 900)
    assert client.get(f"/Videos/{identity}/Trickplay/320/1.jpg").status_code == 404
    assert client.get(f"/Videos/{identity}/Trickplay/999/0.jpg").status_code == 404
    source = client.get(f"/Items/{identity}/PlaybackInfo").json()["MediaSources"][0]
    attachment = source["MediaAttachments"][0]
    assert client.get(attachment["DeliveryUrl"]).content == b"font attachment fixture"
    assert client.get(f"/Videos/{identity}/{identity}/Attachments/0").status_code == 404
    fonts = core[0].data_dir / "fonts"
    fonts.mkdir()
    (fonts / "test.ttf").write_bytes(b"font")
    (fonts / "secret.ttf").symlink_to(path)
    assert [f["Name"] for f in client.get("/FallbackFont/Fonts").json()] == ["test.ttf"]
    assert client.get("/FallbackFont/Fonts/test.ttf").content == b"font"
    assert client.get("/FallbackFont/Fonts/secret.ttf").status_code == 404
    chapter = client.get(f"/Items/{identity}/Images/Chapter/1", params={"width": 240, "format": "png"})
    assert chapter.status_code == 200
    image = Image.open(io.BytesIO(chapter.content))
    assert image.size == (240, 135) and image.format == "PNG"
    assert client.get(f"/Items/{identity}/Images/Chapter/99").status_code == 404
    assert client.get(f"/Items/{identity}/Images/Chapter/0", params={"width": 100000}).status_code == 400


def test_upcoming_and_probe_backfill(video, core):
    client, identity, _, _ = video
    with core[1].session() as db:
        missing = db.scalar(select(Episode).where(Episode.number == 2))
        missing.air_date = "2099-01-01"
        upcoming_id = object_id("episode", missing.id)
        asset = db.scalar(select(MediaAsset))
        asset.probe = {k: v for k, v in asset.probe.items() if k != "chapters"}
    item = client.get(f"/Items/{identity}").json()
    assert len(item["Chapters"]) == 2
    response = client.get("/Shows/Upcoming", params={"enableImages": "false", "enableUserData": "false"})
    assert response.status_code == 200
    assert [i["Id"] for i in response.json()["Items"]] == [upcoming_id]
    assert "UserData" not in response.json()["Items"][0]
    assert "ImageTags" not in response.json()["Items"][0]
    with core[1].session() as db:
        assert len(db.scalar(select(MediaAsset)).probe["chapters"]) == 2
    assert client.get(f"/Items/{upcoming_id}").json()["PlayAccess"] == "None"
    missing = client.get(
        "/Items", params={"recursive": "true", "isMissing": "true", "includeItemTypes": "Episode"}
    ).json()
    assert missing["TotalRecordCount"] == 2


def test_extras_discovery_keeps_primary_and_watched_state_separate(video, core):
    client, identity, path, _ = video
    root = path.parent / "trailers"
    root.mkdir()
    trailer = root / "preview.mkv"
    trailer.write_bytes(path.read_bytes())
    with core[1].session() as db:
        download = db.scalar(select(Download))
        download.plan = {
            **download.plan,
            "files": [{"index": 5, "path": "trailers/preview.mkv", "size": trailer.stat().st_size}],
        }
        download.stats = {**download.stats, "files": [0] * 5 + [trailer.stat().st_size]}
        series_id = object_id("media", db.scalar(select(MediaAsset)).media_id)
    trailers = client.get(f"/Items/{series_id}/LocalTrailers")
    assert trailers.status_code == 200, trailers.text
    extra = trailers.json()[0]
    assert extra["Type"] == "Trailer"
    assert client.get(f"/Items/{series_id}/LocalTrailers").json()[0]["Id"] == extra["Id"]
    assert len(client.get(f"/Items/{identity}/PlaybackInfo").json()["MediaSources"]) == 1
    assert client.get(f"/Videos/{extra['Id']}/stream").content == trailer.read_bytes()
    assert (
        client.post(
            "/Sessions/Playing/Stopped",
            json={"ItemId": extra["Id"], "PositionTicks": 30000000, "PlaySessionId": "extra-play"},
        ).status_code
        == 204
    )
    assert client.get(f"/Items/{extra['Id']}").json()["UserData"]["Played"]
    assert client.get(f"/Items/{extra['Id']}").json()["UserData"]["PlayCount"] == 1
    assert not client.get(f"/Items/{identity}").json()["UserData"]["Played"]
    assert not client.get(f"/Items/{series_id}").json()["UserData"]["Played"]


def test_external_audio_is_remuxed_without_reencoding(video, core, tmp_path):
    client, identity, path, _ = video
    audio = path.with_suffix(".mka")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    with core[1].session() as db:
        link = db.scalar(select(LibraryAsset))
        binding = dict(link.preflight["binding"])
        binding["tracks"] = binding["tracks"] + [{"kind": "audio", "path": audio.name, "language": "ru"}]
        link.preflight = {"binding": binding}
    info = client.get(f"/Items/{identity}/PlaybackInfo")
    assert info.status_code == 200, info.text
    source = info.json()["MediaSources"][0]
    track = next(s for s in source["MediaStreams"] if s["Type"] == "Audio" and s["IsExternal"])
    assert source["DefaultAudioStreamIndex"] == track["Index"]
    assert not source["SupportsDirectPlay"]
    response = client.get(source["DirectStreamUrl"])
    assert response.status_code == 200, response.text
    output = tmp_path / "remux.mkv"
    output.write_bytes(response.content)
    probe = probe_file(output)
    assert probe["ok"]
    assert any(s["codec_name"] == "mpeg4" for s in probe["streams"])
    assert any(s["codec_name"] == "aac" for s in probe["streams"])
    assert client.head(source["DirectStreamUrl"]).status_code == 200
    assert client.get(source["DirectStreamUrl"], headers={"Range": "bytes=0-10"}).status_code == 416
    assert client.get(source["DirectStreamUrl"] + "&startTimeTicks=999999999").status_code == 400
    # Adding a sidecar audio index must not shift the embedded subtitle mapping.
    subtitle = next(s for s in source["MediaStreams"] if s["Type"] == "Subtitle" and not s["IsExternal"])
    assert "Hello world" in client.get(subtitle["DeliveryUrl"]).text
