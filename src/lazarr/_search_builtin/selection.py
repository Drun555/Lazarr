"""Cheap candidate triage. Source claims may exclude, never confirm file bindings."""

import re


UNNUMBERED_TV = r"\b(?:тв|tv)(?=\s*(?:\+\s*specials?\b)?\s*[\])]|$)"
TV_NUMBERS = r"\b(?:tv|тв)[\s._:#№-]*\d{1,3}(?:\s*[-–]\s*(?:(?:tv|тв)[\s._:#№-]*)?\d{1,3})?\b"


def title_seasons(title, *, include_tv=True):
    if not include_tv:
        title = re.sub(TV_NUMBERS, " ", title, flags=re.I)
    reverse = r"\b(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?\s+сезон(?:а|ы|ов)?\b"
    seasons = set()
    for start, end in re.findall(reverse, title, re.I):
        end = end or start
        if int(end) < int(start) or int(end) - int(start) > 100:
            return set()
        seasons.update(range(int(start), int(end) + 1))
    # In '2 сезон: 1-8 серии', the digits after ':' are episodes, not seasons.
    title = re.sub(reverse, " ", title, flags=re.I)
    patterns = [
        r"\b(?:тв|tv|season|сезон)[\s._:#№-]*(\d{1,3})\b",
        r"\b(\d{1,3})(?:st|nd|rd|th)[ ._-]+season\b",
        r"\bs(\d{1,3})(?:e\d+|\b)",
    ]
    seasons.update(int(n) for pattern in patterns for n in re.findall(pattern, title, re.I))
    for start, end in re.findall(
        r"\b(?:тв|tv|season|сезон|s)[\s._:#№-]*(\d{1,3})\s*[-–]\s*(?:(?:тв|tv|season|сезон|s)[\s._:#№-]*)?(\d{1,3})\b",
        title,
        re.I,
    ):
        if int(end) < int(start) or int(end) - int(start) > 100:
            return set()
        seasons.update(range(int(start), int(end) + 1))
    # TV is equivalent to TV-1 for scoring and as a file hint, never a hard constraint.
    if include_tv and re.search(UNNUMBERED_TV, title, re.I):
        seasons.add(1)
    return seasons


def title_episode_coverage(title):
    """Kinozal's '1 сезон: 1-3 серии из 8': 8 is the total, not available coverage."""
    seasons = title_seasons(title)
    if len(seasons) != 1:
        return None, set()
    match = re.search(
        r"\b\d{1,3}\s+сезон\s*:\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?\s+сер(?:ия|ии|ий)\b",
        title,
        re.I,
    )
    if not match:
        return None, set()
    start, end = int(match[1]), int(match[2] or match[1])
    if end < start or end - start > 1000:
        return None, set()
    return next(iter(seasons)), set(range(start, end + 1))


def request_seasons(request):
    aliases = request.media.episode_numbering.get(f"{request.season}:{request.episode}", [])
    return {a["season"] for a in aliases} if aliases else {request.season}


def confirmed_tv_number(candidate, request):
    """TV-N counts broadcasts, not necessarily TMDB seasons. Require name and year."""
    from .matcher import confirmed_named_season, title_matches
    from .provider_utils import season_year

    # Provider episode mappings are authoritative; never invent an offset over them.
    if request.media.episode_numbering.get(f"{request.season}:{request.episode}"):
        return None
    numbers = title_seasons(candidate.title)
    if len(numbers) != 1 or title_seasons(candidate.title, include_tv=False):
        return None
    if not confirmed_named_season(candidate, request):
        # An exact series alias plus this season's year can also identify it.
        # The series' original premiere alone does not identify a later season.
        season = next((s for s in request.media.seasons if s.get("number") == request.season), None)
        year = season_year(season)
        if year is None and request.air_date and re.match(r"^\d{4}-", request.air_date):
            year = int(request.air_date[:4])
        years = {int(v) for v in re.findall(r"\b(?:19|20)\d{2}\b", candidate.title)}
        aliases = [request.media.title, request.media.original_title, *request.media.aliases]
        if year is None or years != {year} or not title_matches(candidate.title, aliases):
            return None
        if any(
            s.get("number") not in {0, request.season} and season_year(s) == year
            for s in request.media.seasons
        ):
            return None
    return next(iter(numbers))


def _hard_reject_reason(candidate, requests, *, detailed=False, allow_preference_mismatch=False):
    from .matcher import resolution

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
    quality = resolution(title)
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
        elif (
            quality
            and not allow_preference_mismatch
            and not request.requirements.min_resolution <= quality <= request.requirements.max_resolution
        ):
            reason = "Заявленное разрешение вне диапазона задачи"
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
            and not allow_preference_mismatch
            and len(complete_audio) == 1
            and not set(request.requirements.audio_languages) <= next(iter(complete_audio))
        ):
            reason = "Полный список аудиодорожек не содержит требуемые языки"
        if reason is None:
            return None
        reasons.append(reason)
    return "; ".join(dict.fromkeys(reasons))


# One strongest signal per group, recomputed at every stage (never accumulated).
SCORE_WEIGHTS = {
    "external_id": 120,
    "season_name_year": 90,
    "series_name_year": 70,
    "name": 20,
    "season_match": 30,
    "season_conflict": -10,
    "episode_claim_match": 10,
    "episode_claim_missing": -10,
    "episode_match": 60,
    "episode_missing": -20,
}
STAGE_THRESHOLDS = {1: 20, 2: 40, 3: 100}


def identity_signal(candidate, request, stage):
    from lazarr.sdk import ScoreSignal
    from .matcher import title_matches, season_title_matches
    from .provider_utils import named_season, season_year

    common = candidate.external_ids.keys() & request.media.external_ids.keys()
    if common:
        mismatch = any(str(candidate.external_ids[k]) != str(request.media.external_ids[k]) for k in common)
        return ScoreSignal(
            group="identity",
            points=-100 if mismatch else SCORE_WEIGHTS["external_id"],
            reason="Внешние идентификаторы различаются" if mismatch else "Совпали внешние идентификаторы",
            source="structured",
        )
    season = named_season(request.media, request.season)
    premiere = season_year(season)
    episode_year = (
        int(request.air_date[:4]) if request.air_date and re.match(r"^\d{4}-", request.air_date) else None
    )
    aliases = [request.media.title, request.media.original_title, *request.media.aliases]
    texts = [(candidate.title, "title")]
    if stage >= 2:
        # Read labelled metadata only; a synopsis may mention unrelated series and dates.
        names = re.findall(
            r"^(?:название|оригинальное название|английское название|title|original title)\s*:\s*(.+)$",
            candidate.description,
            re.M | re.I,
        )
        years = re.findall(
            r"^(?:год(?: выпуска| выхода)?|year|release year)\s*:\s*((?:19|20)\d{2})\b",
            candidate.description,
            re.M | re.I,
        )
        suffix = " " + years[0] if len(set(years)) == 1 else ""
        texts.extend((name + suffix, "description") for name in names)
    best = ScoreSignal(
        group="identity", points=0, reason="Название и год пока не подтверждены", source="title"
    )
    for text, source in texts:
        years = {int(v) for v in re.findall(r"\b(?:19|20)\d{2}\b", text)}
        named = bool(season and season_title_matches(text, season["title"]))
        series = title_matches(text, aliases)
        if named and (premiere or episode_year) in years and len(years) == 1:
            points, reason = (
                SCORE_WEIGHTS["season_name_year"],
                "Совпали собственное название сезона и год его выхода",
            )
        elif series and (
            request.media.year in years or (episode_year in years and request.media.kind == "tv")
        ):
            points, reason = SCORE_WEIGHTS["series_name_year"], "Совпали название произведения и год"
        elif named or series:
            points, reason = SCORE_WEIGHTS["name"], "Название совпало, год не подтверждён"
        else:
            continue
        if points > best.points:
            best = ScoreSignal(group="identity", points=points, reason=reason, source=source)
    return best


def assess_candidate(candidate, request, stage=1, criteria=(), allow_preference_mismatch=False):
    from lazarr.sdk import StageAssessment, ScoreSignal

    identity = identity_signal(candidate, request, stage)
    signals = [identity]
    blockers = []
    hard = _hard_reject_reason(
        candidate, [request], detailed=stage >= 2, allow_preference_mismatch=allow_preference_mismatch
    )
    if hard:
        blockers.append(hard)
    seasons = title_seasons(candidate.title)
    if request.media.kind == "tv" and seasons:
        agrees = bool(seasons & request_seasons(request))
        signals.append(
            ScoreSignal(
                group="season",
                points=SCORE_WEIGHTS["season_match" if agrees else "season_conflict"],
                reason="Номер сезона/показа совпал"
                if agrees
                else "Номер сезона/показа расходится с метаданными",
                source="title",
            )
        )
        # Broadcast numbering is only a weighted hint. Even without a confirmed
        # name/year override, a TV mismatch must reach description/file checks.
        explicit = title_seasons(candidate.title, include_tv=False)
        if explicit and not explicit & request_seasons(request):
            blockers.append("Указан другой сезон")
    if stage < 3:
        from .matcher import episode_numbers

        explicit, numbers, _ = (
            episode_numbers(candidate.title + ".mkv")
            if re.search(r"\bS\d+E\d+", candidate.title, re.I)
            else (None, set(), False)
        )
        if explicit is None:
            explicit, numbers = title_episode_coverage(candidate.title)
        if explicit is not None and numbers:
            coordinates = request.media.episode_numbering.get(f"{request.season}:{request.episode}") or [
                {"season": request.season, "episode": request.episode}
            ]
            covered = any(a["season"] == explicit and a["episode"] in numbers for a in coordinates)
            signals.append(
                ScoreSignal(
                    group="episode",
                    points=SCORE_WEIGHTS["episode_claim_match" if covered else "episode_claim_missing"],
                    reason="Заголовок заявляет нужный эпизод"
                    if covered
                    else "Заголовок не заявляет нужный эпизод; возможен ручной выбор",
                    source="title",
                )
            )
    if stage == 3:
        episode = next((c for c in criteria if c.field == "episode"), None)
        if episode:
            points = (
                SCORE_WEIGHTS["episode_match"]
                if episode.result == "MATCH"
                else SCORE_WEIGHTS["episode_missing"]
                if episode.result == "MISMATCH"
                else 0
            )
            signals.append(
                ScoreSignal(group="episode", points=points, reason=episode.reason, source="torrent")
            )
        for criterion in criteria:
            if criterion.required and criterion.result != "MATCH":
                blockers.append(criterion.reason)
    total = sum(signal.points for signal in signals)
    threshold = STAGE_THRESHOLDS[stage]
    return StageAssessment(
        stage=stage,
        total=total,
        threshold=threshold,
        passed=total >= threshold and not blockers,
        signals=signals,
        blockers=list(dict.fromkeys(blockers)),
    )


def reject_reason(candidate, requests, *, detailed=False, allow_preference_mismatch=False):
    assessments = [
        assess_candidate(
            candidate, r, 2 if detailed else 1, allow_preference_mismatch=allow_preference_mismatch
        )
        for r in requests
    ]
    if any(a.passed for a in assessments):
        return None
    return "; ".join(
        dict.fromkeys(
            reason
            for a in assessments
            for reason in (
                a.blockers or [f"Недостаточно подтверждений: {a.total} баллов, порог {a.threshold}"]
            )
        )
    )


def candidate_rank(candidate, requests):
    from .matcher import resolution

    quality = resolution(candidate.title)
    score = max((assess_candidate(candidate, r).total for r in requests), default=0)
    return (
        -score,
        -(quality or 0),
        -(candidate.seeds or 0),
        candidate.size or 2**63,
        candidate.provider,
        candidate.id,
    )
