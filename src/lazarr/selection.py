"""Cheap candidate triage. Source claims may exclude, never confirm file bindings."""

import re


def title_seasons(title):
    patterns = [
        r"\b(?:тв|tv|season|сезон)[\s._:#№-]*(\d{1,3})\b",
        r"\b(\d{1,3})(?:st|nd|rd|th)[ ._-]+season\b",
        r"\bs(\d{1,3})(?:e\d+|\b)",
    ]
    seasons = {int(n) for pattern in patterns for n in re.findall(pattern, title, re.I)}
    for start, end in re.findall(
        r"\b(?:тв|tv|season|сезон|s)[\s._:#№-]*(\d{1,3})\s*[-–]\s*(?:(?:тв|tv|season|сезон|s)[\s._:#№-]*)?(\d{1,3})\b",
        title,
        re.I,
    ):
        if int(end) < int(start) or int(end) - int(start) > 100:
            return set()
        seasons.update(range(int(start), int(end) + 1))
    return seasons


def request_seasons(request):
    aliases = request.media.episode_numbering.get(f"{request.season}:{request.episode}", [])
    return {a["season"] for a in aliases} if aliases else {request.season}


def reject_reason(candidate, requests, *, detailed=False):
    from lazarr.matcher import resolution, episode_numbers

    title = candidate.title
    category = " ".join(str(e.value) for e in candidate.evidence if e.field == "category")
    unrelated = r"\b(?:manga|soundtrack|OST|EPUB|FB2|DOCX|PDF|NSP)\b|\[(?:Art|Nintendo Switch)\]|\b(?:манга|аудиокниги|саундтреки)\b"
    if re.search(unrelated, title + " " + category, re.I):
        return "Тип раздачи: книги, изображения, музыка или игры"
    if re.search(r"\[RUS\(Dub\)\]", title, re.I) and re.search(r"\[\d{4},\s*(?:ААС|AAC)\]", title, re.I):
        return "Раздача содержит отдельное аудио без видео"
    if (
        re.search(r"\b(?:MP3|FLAC)\b", title, re.I)
        and re.search(r"\b(?:tracks|lossless|kbps)\b", title, re.I)
        and not re.search(r"\b(?:BDRip|WEBRip|WEB-DL|BDRemux|\d{3,4}p)\b", title, re.I)
    ):
        return "Музыкальная или аудиораздача без видео"
    if "[DL]" in title and "[P]" in title and "[Scene]" in title:
        return "Программное обеспечение или игра"
    seasons = title_seasons(title)
    quality = resolution(title)
    explicit_season, explicit_episodes, _ = (
        episode_numbers(title + ".mkv") if re.search(r"\bS\d+E\d+", title, re.I) else (None, set(), False)
    )
    complete_audio = {
        frozenset(e.value)
        for e in candidate.evidence
        if e.field == "audio_languages"
        and e.complete
        and e.scope == "all_video_files"
        and isinstance(e.value, list)
    }
    reasons = []
    for request in requests:
        reason = None
        if request.media.kind == "tv" and re.search(r"\[Movie\]|\(Фильм\)", title, re.I):
            reason = "Фильм вместо эпизодов сериала"
        elif request.media.kind == "tv" and seasons and not seasons & request_seasons(request):
            reason = "Указан другой сезон"
        elif (
            explicit_season is not None
            and explicit_episodes
            and not any(
                a["season"] == explicit_season and a["episode"] in explicit_episodes
                for a in request.media.episode_numbering.get(
                    f"{request.season}:{request.episode}",
                    [{"season": request.season, "episode": request.episode}],
                )
            )
        ):
            reason = "Указанные эпизоды не пересекаются с запросом"
        elif (
            quality
            and not request.requirements.min_resolution <= quality <= request.requirements.max_resolution
        ):
            reason = "Заявленное разрешение вне диапазона задачи"
        elif quality and request.current_resolution and quality <= request.current_resolution:
            reason = "Разрешение не улучшает полученную версию"
        elif any(
            str(candidate.external_ids[k]) != str(request.media.external_ids[k])
            for k in candidate.external_ids.keys() & request.media.external_ids.keys()
        ):
            reason = "Внешние идентификаторы различаются"
        elif (
            detailed
            and request.requirements.keyword.strip().casefold()
            not in (title + "\n" + candidate.description).casefold()
        ):
            reason = "Обязательная фраза отсутствует в заголовке и описании"
        # Only an explicitly complete, release-wide declaration can prove absence.
        elif (
            detailed
            and len(complete_audio) == 1
            and not set(request.requirements.audio_languages) <= next(iter(complete_audio))
        ):
            reason = "Полный список аудиодорожек не содержит требуемые языки"
        if reason is None:
            return None
        reasons.append(reason)
    return "; ".join(dict.fromkeys(reasons))


def candidate_rank(candidate, requests):
    from lazarr.matcher import resolution

    quality = resolution(candidate.title)
    expected = set().union(*(request_seasons(r) for r in requests))
    return (
        0 if title_seasons(candidate.title) & expected else 1,
        -(quality or 0),
        -(candidate.seeds or 0),
        candidate.size or 2**63,
        candidate.provider,
        candidate.id,
    )


def can_improve(candidate, requests, best):
    from lazarr.matcher import resolution

    quality = resolution(candidate.title)
    return any(
        best.get(r.id, r.current_resolution or 0)
        < min(quality or r.requirements.max_resolution, r.requirements.max_resolution)
        for r in requests
    )
