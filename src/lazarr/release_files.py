"""Translate file bindings between torrent snapshots by path and size, never by index."""

from copy import deepcopy


def file_identity(file):
    return file["path"], file.get("size", 0)


def remap_binding(binding, source, target):
    if not binding:
        return None
    source = {file["index"]: file for file in source}
    target = {file_identity(file): file for file in target}
    result = deepcopy(binding)
    video = source.get(binding["video_index"])
    match = target.get(file_identity(video)) if video else None
    if match is None:
        return None
    result["video_index"] = match["index"]
    result["video_path"] = match["path"]
    for track in result.get("tracks", []):
        if track.get("file_index") is None:
            continue
        file = source.get(track["file_index"])
        match = target.get(file_identity(file)) if file else None
        if match is None:
            return None
        track["file_index"], track["path"] = match["index"], match["path"]
    return result


def mapping_catalog(revision, files, snapshots):
    """Expose latest files plus selected legacy files that vanished or changed."""
    result = [{**file, "revision": revision, "source_index": file["index"]} for file in files]
    known = {file_identity(file) for file in result}
    index = max((file["index"] for file in result), default=-1) + 1
    for snapshot_revision, snapshot_files in snapshots:
        for file in snapshot_files:
            if file_identity(file) not in known:
                result.append(
                    {**file, "index": index, "revision": snapshot_revision, "source_index": file["index"]}
                )
                known.add(file_identity(file))
                index += 1
    return result
