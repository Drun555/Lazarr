from pathlib import PurePosixPath
from .matcher import AUDIO, SUBTITLE, episode_numbers, playable_video, stem_key


def file_kind(file):
    extension = PurePosixPath(file.path).suffix.lower()
    return (
        "video"
        if playable_video(file)
        else "audio"
        if extension in AUDIO
        else "subtitle"
        if extension in SUBTITLE
        else "other"
    )


def related_files(files, bindings=()):
    """Only attach a sidecar when exactly one video matches, even without episode IDs."""
    videos = [file for file in files if playable_video(file)]
    groups = {video.index: [] for video in videos}
    saved = {}
    for binding in bindings:
        if binding.get("video_index") not in groups:
            continue
        for track in binding.get("tracks", []):
            if track.get("file_index") is not None:
                saved.setdefault(track["file_index"], set()).add(binding["video_index"])
    for file in files:
        if file_kind(file) not in {"audio", "subtitle"}:
            continue
        number = episode_numbers(file.path)
        matches = [
            video.index
            for video in videos
            if stem_key(file.path) == stem_key(video.path)
            or (number[1] and number == episode_numbers(video.path))
        ]
        if file.index in saved:
            matches = sorted(saved[file.index])
        if len(matches) == 1:
            groups[matches[0]].append(file.index)
    return groups
