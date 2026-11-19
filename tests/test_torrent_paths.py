import pytest

from lazarr.sdk import DownloadSource, TorrentFile, safe_relative_path


@pytest.mark.parametrize(
    "path",
    [
        "[Kawaiika-Raws] Owarimonogatari/Fonts/Brush Script LET Plain:.ttf",
        "Show: Season 1/Show.S01E01.mkv",
        "Brush Script LET Plain:.ttf",
    ],
)
def test_torrent_paths_preserve_colons(path):
    assert TorrentFile(index=0, path=path, size=1).path == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../escape.mkv",
        "Show/../../escape.mkv",
        r"Show\..\escape.mkv",
        "/tmp/escape.mkv",
        r"\tmp\escape.mkv",
        "C:/escape.mkv",
        r"C:\escape.mkv",
        "C:escape.mkv",
        "c:escape.mkv",
        r"\\server\share\escape.mkv",
        "//server/share/escape.mkv",
        "Show/bad\x00.mkv",
    ],
)
def test_torrent_paths_reject_unsafe_paths(path):
    with pytest.raises(ValueError, match="Unsafe torrent path"):
        safe_relative_path(path)


def test_torrent_paths_normalize_backslashes():
    assert safe_relative_path(r"Show\Fonts\Brush Script LET Plain:.ttf") == (
        "Show/Fonts/Brush Script LET Plain:.ttf"
    )


def test_inspect_torrent_with_bundled_font_containing_colon(tmp_path):
    lt = pytest.importorskip("libtorrent")
    from lazarr.torrent import LibtorrentEngine

    # Exercise the metadata reader used before manual file selection. No peers
    # or actual download are needed to reproduce the validation failure.
    paths = [
        "[Kawaiika-Raws] Owarimonogatari/Show.S01E01.mkv",
        "[Kawaiika-Raws] Owarimonogatari/Fonts/Brush Script LET Plain:.ttf",
    ]
    torrent = lt.bencode(
        {
            "info": {
                "name": "[Kawaiika-Raws] Owarimonogatari",
                "piece length": 16384,
                "pieces": b"\x00" * 20,
                "files": [{"length": 1, "path": path.split("/")[1:]} for path in paths],
            }
        }
    )
    engine = LibtorrentEngine(tmp_path / "state", "127.0.0.1:0", local_only=True)
    try:
        metadata = engine.inspect(DownloadSource(torrent=torrent))
        assert [file.path for file in metadata.files] == paths
    finally:
        engine.close()
