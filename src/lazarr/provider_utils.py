"""Compatibility facade for the application search engine."""

from lazarr.search_runtime import engine_call, current_engine


def size_bytes(*args, **kwargs):
    return engine_call("provider_utils", "size_bytes", *args, **kwargs)


def mediainfo_audio_evidence(*args, **kwargs):
    return engine_call("provider_utils", "mediainfo_audio_evidence", *args, **kwargs)


def description_evidence(*args, **kwargs):
    return engine_call("provider_utils", "description_evidence", *args, **kwargs)


def title_subtitle_evidence(*args, **kwargs):
    return engine_call("provider_utils", "title_subtitle_evidence", *args, **kwargs)


def _title_key(*args, **kwargs):
    return engine_call("provider_utils", "_title_key", *args, **kwargs)


def search_title(*args, **kwargs):
    return engine_call("provider_utils", "search_title", *args, **kwargs)


def search_titles(*args, **kwargs):
    return engine_call("provider_utils", "search_titles", *args, **kwargs)


def search_queries(media, season=None, year=None):
    build = getattr(current_engine().provider_utils, "search_queries", None)
    if build is not None:
        return build(media, season=season, year=year)
    titles = search_titles(media)
    return [f"{title} {year}" for title in titles] + titles if year else titles
