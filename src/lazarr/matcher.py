"""Compatibility facade for the replaceable search engine."""

from lazarr.search_runtime import engine_call, current_engine


def __getattr__(name):
    if name in {"VIDEO", "AUDIO", "SUBTITLE", "FORCED_SUBTITLE_MARKERS", "FULL_SUBTITLE_MARKERS"}:
        return getattr(current_engine().matcher, name)
    raise AttributeError(name)


def normalized(*args, **kwargs):
    return engine_call("matcher", "normalized", *args, **kwargs)


def subtitle_title_is_forced(*args, **kwargs):
    return engine_call("matcher", "subtitle_title_is_forced", *args, **kwargs)


def resolution(*args, **kwargs):
    return engine_call("matcher", "resolution", *args, **kwargs)


def episode_numbers(*args, **kwargs):
    return engine_call("matcher", "episode_numbers", *args, **kwargs)


def file_language(*args, **kwargs):
    return engine_call("matcher", "file_language", *args, **kwargs)


def stem_key(*args, **kwargs):
    return engine_call("matcher", "stem_key", *args, **kwargs)


def classify_external_subtitles(*args, **kwargs):
    return engine_call("matcher", "classify_external_subtitles", *args, **kwargs)


def playable_video(*args, **kwargs):
    return engine_call("matcher", "playable_video", *args, **kwargs)


def first_season_by_year_and_count(*args, **kwargs):
    return engine_call("matcher", "first_season_by_year_and_count", *args, **kwargs)


class Matcher:
    def __getattr__(self, name):
        return getattr(current_engine().matcher.Matcher(), name)
