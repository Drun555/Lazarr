"""Compatibility facade for the replaceable search engine."""

from lazarr.search_runtime import engine_call


def title_seasons(*args, **kwargs):
    return engine_call("selection", "title_seasons", *args, **kwargs)


def title_episode_coverage(*args, **kwargs):
    return engine_call("selection", "title_episode_coverage", *args, **kwargs)


def request_seasons(*args, **kwargs):
    return engine_call("selection", "request_seasons", *args, **kwargs)


def reject_reason(*args, **kwargs):
    return engine_call("selection", "reject_reason", *args, **kwargs)


def candidate_rank(*args, **kwargs):
    return engine_call("selection", "candidate_rank", *args, **kwargs)
