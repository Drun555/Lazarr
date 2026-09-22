import shutil
import subprocess

import pytest

from lazarr.subtitle_language import detect_language_from_text, detect_subtitle_language


ENGLISH = (
    "We are going to the station together. The train will arrive in a few minutes. "
    "Please bring your ticket and wait beside the entrance. I can see our friends "
    "walking toward us now. They have already found a place for everyone to sit. "
) * 3
RUSSIAN = (
    "Мы вместе идём на станцию. Поезд прибудет через несколько минут. "
    "Пожалуйста, возьми билет и подожди рядом со входом. Наши друзья уже идут сюда. "
    "Они нашли свободные места для всех и скоро мы отправимся в путешествие. "
) * 3


def test_detects_text_but_rejects_short_or_empty_subtitles():
    assert detect_language_from_text(ENGLISH.encode()) == "en"
    assert detect_language_from_text(RUSSIAN.encode()) == "ru"
    assert detect_language_from_text(b"1\n00:00:01,000 --> 00:00:02,000\nHello!\n") == "und"
    assert detect_language_from_text(b"") == "und"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
def test_detects_external_and_embedded_text_streams(tmp_path):
    external = tmp_path / "subtitles.srt"
    external.write_text("1\n00:00:01,000 --> 00:00:10,000\n" + RUSSIAN + "\n", encoding="utf-8")
    assert detect_subtitle_language(external) == "ru"

    embedded = tmp_path / "episode.mkv"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=16x16:d=11",
            "-i",
            str(external),
            "-c:v",
            "mpeg4",
            "-c:s",
            "srt",
            "-shortest",
            str(embedded),
        ],
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert detect_subtitle_language(embedded, 1, "subrip") == "ru"
    assert detect_subtitle_language(embedded, 1, "hdmv_pgs_subtitle") == "und"
