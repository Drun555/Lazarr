"""Conservative language detection for untagged text subtitle streams."""

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from langdetect import DetectorFactory, LangDetectException, detect_langs

from lazarr.languages import LABELS, language


DetectorFactory.seed = 0
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd"}
TEXT_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


def _subtitle_text(data):
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]*>|\{[^}]*\}", " ", text)
    text = re.sub(r"(?m)^\s*(?:\d+|\d\d:\d\d:\d\d[.,]\d+.*|WEBVTT)\s*$", " ", text)
    text = re.sub(r"[^\w\s'-]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def detect_language_from_text(data):
    text = _subtitle_text(data)
    if len(text) < 200 or len(re.findall(r"\w+", text)) < 35:
        return "und"
    try:
        guesses = detect_langs(text[:10000])
    except LangDetectException:
        return "und"
    if not guesses or guesses[0].prob < 0.90:
        return "und"
    result = language(guesses[0].lang)
    return result if result in LABELS else "und"


@lru_cache(maxsize=256)
def _detect_file(path, size, modified_ns, stream_index):
    del size, modified_ns
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path,
                "-map",
                f"0:{stream_index}" if stream_index is not None else "0:s:0",
                "-t",
                "1200",
                "-f",
                "srt",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "und"
    return detect_language_from_text(result.stdout) if result.returncode == 0 else "und"


def detect_subtitle_language(path, stream_index=None, codec=None):
    """Return a supported language code, or und when text is insufficient."""
    path = Path(path)
    if stream_index is None and path.suffix.lower() not in TEXT_SUFFIXES:
        return "und"
    if stream_index is not None and codec not in TEXT_CODECS:
        return "und"
    try:
        stat = path.stat()
    except OSError:
        return "und"
    return _detect_file(str(path), stat.st_size, stat.st_mtime_ns, stream_index)
