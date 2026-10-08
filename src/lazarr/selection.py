"""Compatibility facade for the application search engine."""

from lazarr.search_runtime import engine_call, current_engine


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


def assess_candidate(*args, **kwargs):
    assess = getattr(current_engine().selection, "assess_candidate", None)
    return assess(*args, **kwargs) if assess else None
