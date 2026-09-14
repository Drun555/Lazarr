"""Worker's deterministic file matcher. Provider claims never create file bindings."""

import re
import unicodedata
from pathlib import PurePosixPath
from lazarr.languages import language_name
from lazarr.provider_utils import title_subtitle_evidence
from lazarr.sdk import (
    Candidate,
    TorrentFile,
    SubtaskRequest,
    Criterion,
    MatchResult,
    TrackBinding,
    FileBinding,
    SubtaskEvaluation,
    EvaluationReport,
    DownloadPlan,
    language,
)

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".webm", ".mov", ".mpg", ".mpeg"}
AUDIO = {".mka", ".ac3", ".eac3", ".aac", ".flac", ".dts", ".mp3", ".ogg", ".wav", ".m4a"}
SUBTITLE = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}


def normalized(value):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def resolution(value):
    hits = {int(v) for v in re.findall(r"(?<!\d)(480|576|720|1080|1440|2160|4320)[pi]\b", value, re.I)}
    if re.search(r"\b4k\b", value, re.I):
        hits.add(2160)
    return next(iter(hits)) if len(hits) == 1 else None


def episode_numbers(path, aliases=()):
    """Return (season, numbers, absolute). Never guess season offsets for anime."""
    name = PurePosixPath(path).stem
    matches = list(
        re.finditer(r"[sS](\d{1,3})[ ._-]*[eE](\d{1,4})(?:[ ._-]*(?:[eE]|-[eE]?)(\d{1,4}))?", name)
    )
    if matches:
        seasons = {int(m[1]) for m in matches}
        numbers = set()
        for m in matches:
            start, end = int(m[2]), int(m[3] or m[2])
            if end < start or end - start > 100:
                return None, set(), False
            numbers.update(range(start, end + 1))
        return next(iter(seasons)) if len(seasons) == 1 else None, numbers, False
    match = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", name, re.I)
    if match:
        return int(match[1]), {int(match[2])}, False
    parent = re.search(r"(?:season|сезон|s)[ ._-]*(\d{1,3})\b", str(PurePosixPath(path).parent), re.I)
    number = re.search(r"(?:episode|эпизод|серия|ep|e)[ ._-]*(\d{1,4})(?!\d)", name, re.I)
    if parent and number:
        return int(parent[1]), {int(number[1])}, False
    cleaned = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", name)
    # Anime filenames may state "2nd Season - Part 2 - 14". Part is not
    # an episode offset: only bind the literal season and trailing episode.
    seasons = {
        int(m[1] or m[2])
        for m in re.finditer(
            r"\b(\d{1,3})(?:st|nd|rd|th)[ ._-]+season\b|\b(?:season|сезон)[ ._-]*(\d{1,3})\b",
            path,
            re.I,
        )
    }
    trailing = re.search(r"(?:^|[ ._-])(\d{1,4})\s*$", cleaned)
    without_labels = re.sub(r"\b(?:part|часть)[ ._-]*\d+\b", " ", cleaned, flags=re.I)
    without_labels = re.sub(
        r"\b\d+(?:st|nd|rd|th)[ ._-]+season\b|\b(?:season|сезон)[ ._-]*\d+\b",
        " ",
        without_labels,
        flags=re.I,
    )
    if len(seasons) == 1 and trailing and re.search(r"(?:^|[ ._-])" + trailing[1] + r"\s*$", without_labels):
        return next(iter(seasons)), {int(trailing[1])}, False
    if seasons:
        return next(iter(seasons)) if len(seasons) == 1 else None, set(), False
    for alias in sorted(aliases, key=len, reverse=True):
        if alias:
            cleaned = re.sub(re.escape(alias), " ", cleaned, flags=re.I)
    values = re.findall(r"(?:^|[ ._-])(\d{1,3})(?:v\d)?(?=$|[ ._-])", cleaned)
    values = {int(v) for v in values if int(v) not in {264, 265, 480, 576, 720}}
    if len(values) == 1:
        return (int(parent[1]) if parent else None), values, not bool(parent)
    return None, set(), False


def file_language(path):
    # Directory names such as Audio/RUS are useful; codec and unrelated two-letter words are not.
    known = {"ru", "en", "ja", "uk", "de", "fr", "es", "it", "zh", "ko", "pt", "pl", "ar", "hi", "tr", "nl"}
    values = {language(token) for token in re.findall(r"[a-zа-яё]+", path.lower())}
    values &= known
    return next(iter(values)) if len(values) == 1 else "und"


def stem_key(path):
    stem = PurePosixPath(path).stem
    tokens = [
        v
        for v in normalized(stem).split()
        if language(v) == "und" and v not in {"audio", "subs", "subtitles", "forced", "sdh"}
    ]
    return " ".join(tokens)


def playable_video(file):
    return PurePosixPath(file.path).suffix.lower() in VIDEO and not re.search(
        r"(?:^|[/ ._-])(sample|trailer|extras|bonus|opening|ending|ncop|nced)(?:[/ ._-]|$)", file.path, re.I
    )


class Matcher:
    def identity(self, candidate, request):
        common = set(candidate.external_ids) & set(request.media.external_ids)
        if common:
            mismatch = any(
                str(candidate.external_ids[k]) != str(request.media.external_ids[k]) for k in common
            )
            return Criterion(
                field="identity",
                result=MatchResult.MISMATCH if mismatch else MatchResult.MATCH,
                reason="Внешние идентификаторы различаются" if mismatch else "Совпали внешние идентификаторы",
            )
        aliases = [request.media.title, request.media.original_title, *request.media.aliases]
        segments = [
            normalized(part) for part in re.split(r"[/|]", re.sub(r"\[[^]]*\]", " ", candidate.title))
        ]
        found = False
        for alias in aliases:
            name = normalized(alias)
            if not name:
                continue
            for segment in segments:
                if segment == name:
                    found = True
                elif segment.startswith(name + " "):
                    tail = segment[len(name) :].strip()
                    # A sequel or a longer title must not match merely by containing an alias.
                    if re.match(
                        r"(?:(?:19|20)\d{2}|s\d+|season\b|сезон\b|тв\b|tv\b|\d+(?:st|nd|rd|th) season\b|\d+(?: \d+)? сезон(?:а|ы|ов)?\b|\d{3,4}[pi]\b|4k\b)",
                        tail,
                    ):
                        found = True
        years = {int(v) for v in re.findall(r"\b(?:19|20)\d{2}\b", candidate.title)}
        if not found:
            return Criterion(
                field="identity", result=MatchResult.UNKNOWN, reason="Название произведения не подтверждено"
            )
        if years and request.media.year and request.media.year not in years:
            if request.media.kind == "tv":
                from lazarr.selection import title_seasons, request_seasons

                episode_year = (
                    int(request.air_date[:4])
                    if request.air_date and re.match(r"^\d{4}-", request.air_date)
                    else None
                )
                if episode_year in years and title_seasons(candidate.title) & request_seasons(request):
                    return Criterion(
                        field="identity",
                        result=MatchResult.MATCH,
                        reason="Совпали название, сезон и год выхода эпизода",
                    )
                return Criterion(
                    field="identity",
                    result=MatchResult.UNKNOWN,
                    reason="Название совпало; год раздачи может относиться к отдельному сезону, нужен ID или ручное подтверждение",
                )
            return Criterion(
                field="identity", result=MatchResult.MISMATCH, reason="Год произведения отличается"
            )
        if request.media.year and request.media.year in years:
            return Criterion(field="identity", result=MatchResult.MATCH, reason="Совпали название и год")
        return Criterion(
            field="identity",
            result=MatchResult.UNKNOWN,
            reason="Название совпало, но год или ID не подтверждены",
        )

    def _video_matches(self, file, request):
        if request.media.kind == "movie":
            return True
        season, numbers, absolute = episode_numbers(
            file.path, [request.media.title, request.media.original_title, *request.media.aliases]
        )
        if absolute:
            return request.absolute_number is not None and request.absolute_number in numbers
        canonical = f"{request.season}:{request.episode}"
        aliases = request.media.episode_numbering.get(canonical, [])
        # If an explicit alternate ordering uses this season, use it consistently:
        # TMDB's S01E39 must not also match a release's alternate S01E39.
        alternate_season = any(
            a.get("season") == season for values in request.media.episode_numbering.values() for a in values
        )
        if aliases and alternate_season:
            return any(a.get("season") == season and a.get("episode") in numbers for a in aliases)
        return season == request.season and request.episode in numbers

    def _scoped_evidence(self, candidate, video, videos, field):
        return [
            e
            for e in candidate.evidence
            if e.field == field
            and (
                (e.scope == "file" and e.file_path == video.path)
                or e.scope == "all_video_files"
                or (e.scope == "release" and len(videos) == 1)
            )
        ]

    def evaluate(
        self, candidate: Candidate, requests: list[SubtaskRequest], files: list[TorrentFile], infohash=""
    ):
        title_evidence = title_subtitle_evidence(candidate.title)
        if title_evidence:
            candidate = candidate.model_copy(update={"evidence": [*candidate.evidence, *title_evidence]})
        videos = [file for file in files if playable_video(file)]
        reports = []
        for request in requests:
            criteria = [self.identity(candidate, request)]
            possible = [v for v in videos if self._video_matches(v, request)]
            video = possible[0] if len(possible) == 1 else None
            numbered = [
                episode_numbers(v.path, [request.media.title, request.media.original_title]) for v in videos
            ]
            absent_episode = (
                request.media.kind == "tv"
                and not possible
                and bool(numbered)
                and all(
                    numbers and (request.absolute_number is not None if absolute else season is not None)
                    for season, numbers, absolute in numbered
                )
            )
            criteria.append(
                Criterion(
                    field="episode",
                    result=MatchResult.MATCH
                    if video
                    else MatchResult.MISMATCH
                    if absent_episode
                    else MatchResult.UNKNOWN,
                    reason="Видео сопоставлено с подтаской"
                    if video
                    else "В торренте нет запрошенного эпизода"
                    if absent_episode
                    else "Нет однозначного соответствия видео и эпизода",
                )
            )
            keyword = request.requirements.keyword.strip().casefold()
            criteria.append(
                Criterion(
                    field="keyword",
                    result=MatchResult.MATCH
                    if not keyword or keyword in f"{candidate.title}\n{candidate.description}".casefold()
                    else MatchResult.MISMATCH,
                    reason="Обязательная фраза найдена или не задана"
                    if not keyword or keyword in f"{candidate.title}\n{candidate.description}".casefold()
                    else "Обязательная фраза отсутствует",
                )
            )
            binding = None
            if video:
                raw_resolution = resolution(video.path)
                resolution_evidence = self._scoped_evidence(candidate, video, videos, "resolution")
                claimed = {int(e.value) for e in resolution_evidence if str(e.value).isdigit()}
                if raw_resolution:
                    claimed.add(raw_resolution)
                if not claimed:
                    title_resolution = resolution(candidate.title)
                    if title_resolution:
                        claimed.add(title_resolution)
                quality = next(iter(claimed)) if len(claimed) == 1 else None
                quality_result = (
                    MatchResult.UNKNOWN
                    if quality is None
                    else (
                        MatchResult.MATCH
                        if request.requirements.min_resolution
                        <= quality
                        <= request.requirements.max_resolution
                        else MatchResult.MISMATCH
                    )
                )
                if (
                    request.current_resolution
                    and quality is not None
                    and quality <= request.current_resolution
                ):
                    quality_result = MatchResult.MISMATCH
                criteria.append(
                    Criterion(
                        field="resolution",
                        result=quality_result,
                        reason=f"Разрешение: {quality}p"
                        if quality
                        else "Разрешение неизвестно или сведения противоречат друг другу",
                        evidence=resolution_evidence,
                    )
                )
                tracks = []
                ambiguities = set()
                for file in files:
                    extension = PurePosixPath(file.path).suffix.lower()
                    kind = "audio" if extension in AUDIO else "subtitle" if extension in SUBTITLE else None
                    if not kind:
                        continue
                    # Associate sidecars with videos, not just requested episode numbers.
                    matching = []
                    for v in videos:
                        same_stem = stem_key(file.path) == stem_key(v.path)
                        file_number = episode_numbers(
                            file.path, [request.media.title, request.media.original_title]
                        )
                        video_number = episode_numbers(
                            v.path, [request.media.title, request.media.original_title]
                        )
                        same_episode = bool(file_number[1]) and file_number == video_number
                        if same_stem or same_episode:
                            matching.append(v.index)
                    lang = file_language(file.path)
                    if matching == [video.index]:
                        tracks.append(
                            TrackBinding(kind=kind, language=lang, file_index=file.index, path=file.path)
                        )
                    elif video.index in matching:
                        ambiguities.add((kind, lang))
                for kind, field in [("audio", "audio_languages"), ("subtitle", "subtitle_languages")]:
                    evidence = self._scoped_evidence(candidate, video, videos, field)
                    described_external = {
                        language(v)
                        for e in evidence
                        if e.delivery == "external" and isinstance(e.value, list)
                        for v in e.value
                    } - {"und"}
                    title_external = set()
                    if kind == "subtitle":
                        title_external = {
                            language(value)
                            for item in candidate.evidence
                            if item.field == field
                            and item.source == "title"
                            and item.delivery == "external"
                            and isinstance(item.value, list)
                            for value in item.value
                        } - {"und"}
                    external = described_external | title_external
                    inferred = next(iter(external)) if len(external) == 1 else None
                    if inferred:
                        for track in tracks:
                            if track.kind == kind and track.language == "und":
                                track.language = inferred
                                track.language_source = (
                                    "title" if inferred in title_external else "description"
                                )
                    for item in evidence:
                        if item.delivery != "external" and isinstance(item.value, list):
                            for lang in item.value:
                                tracks.append(TrackBinding(kind=kind, language=language(lang), embedded=True))
                audio = {t.language for t in tracks if t.kind == "audio"}
                wanted_audio = set(request.requirements.audio_languages)
                missing_audio = wanted_audio - audio
                evidence = self._scoped_evidence(candidate, video, videos, "audio_languages")
                complete_evidence = [e for e in evidence if e.complete]
                conflicting = (
                    len({tuple(sorted(e.value)) for e in complete_evidence if isinstance(e.value, list)}) > 1
                )
                unknown_audio = conflicting or any(
                    ("audio", lang) in ambiguities
                    for lang in wanted_audio
                    - {t.language for t in tracks if t.kind == "audio" and t.embedded}
                )
                audio_result = (
                    MatchResult.UNKNOWN
                    if unknown_audio
                    else MatchResult.MATCH
                    if not missing_audio
                    else MatchResult.MISMATCH
                    if complete_evidence
                    else MatchResult.UNKNOWN
                )
                criteria.append(
                    Criterion(
                        field="audio",
                        result=audio_result,
                        reason="Требуемые языки аудио найдены; фактическая проверка после загрузки"
                        if audio_result == MatchResult.MATCH
                        else "Не подтверждены языки или связь аудио: "
                        + ", ".join(language_name(v) for v in sorted(missing_audio or wanted_audio)),
                        evidence=evidence,
                    )
                )
                wanted_subs = set(request.requirements.subtitle_languages)
                subtitle_evidence = self._scoped_evidence(candidate, video, videos, "subtitle_languages")
                for item in candidate.evidence:
                    if (
                        item.field == "subtitle_languages"
                        and item.source == "title"
                        and item not in subtitle_evidence
                    ):
                        subtitle_evidence.append(item)
                present_subs = {
                    t.language
                    for t in tracks
                    if t.kind == "subtitle" and ("subtitle", t.language) not in ambiguities
                }
                missing_subs = sorted(wanted_subs - present_subs)
                criteria.append(
                    Criterion(
                        field="subtitles",
                        required=False,
                        result=MatchResult.UNKNOWN if missing_subs else MatchResult.MATCH,
                        reason="Нет субтитров: " + ", ".join(language_name(v) for v in missing_subs)
                        if missing_subs
                        else "Субтитры доступны или не запрошены",
                        evidence=subtitle_evidence,
                    )
                )
                tracks = [
                    t
                    for t in tracks
                    if (
                        t.kind == "audio"
                        and t.language in wanted_audio
                        or t.kind == "subtitle"
                        and (t.file_index is not None or t.language in wanted_subs)
                    )
                    and (t.kind, t.language) not in ambiguities
                ]
                binding = FileBinding(
                    subtask_id=request.id,
                    video_index=video.index,
                    video_path=video.path,
                    episode_order=(request.season or 0) * 10000 + (request.episode or 0),
                    tracks=tracks,
                    resolution=quality,
                    missing_subtitle_languages=missing_subs,
                )
            required = [c.result for c in criteria if c.required]
            outcome = (
                MatchResult.MISMATCH
                if MatchResult.MISMATCH in required
                else (MatchResult.UNKNOWN if MatchResult.UNKNOWN in required else MatchResult.MATCH)
            )
            reports.append(
                SubtaskEvaluation(subtask_id=request.id, result=outcome, criteria=criteria, binding=binding)
            )
        bindings = [r.binding for r in reports if r.result == MatchResult.MATCH and r.binding]
        return EvaluationReport(
            evaluations=reports,
            plan=DownloadPlan(infohash=infohash, bindings=bindings, files=files) if bindings else None,
        )
