"""Shared text extraction helpers. Claims remain unverified until Worker evaluation."""

import re
from lazarr.sdk import Evidence
from lazarr.languages import extract_languages


def size_bytes(text):
    match = re.search(r"([\d.,]+)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|ТБ|ГБ|МБ|КБ|B)", text, re.I)
    if not match:
        return None
    amount, unit = match.groups()
    unit = unit.lower()
    powers = {"t": 4, "g": 3, "m": 2, "k": 1, "т": 4, "г": 3, "м": 2, "к": 1, "b": 0}
    return int(float(amount.replace(",", ".")) * 1024 ** powers.get(unit[0], 0))


def mediainfo_audio_evidence(text):
    """Read languages only from MediaInfo audio sections, never video metadata."""
    dumps = []
    current = None
    section = None
    excerpts = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"(?:General|Общее)", stripped, re.I):
            if current:
                dumps.append(current)
            current = set()
            section = None
            continue
        if current is None:
            continue
        if re.fullmatch(r"(?:Audio|Аудио)(?:\s*#\d+)?", stripped, re.I):
            section = "audio"
            continue
        if re.fullmatch(
            r"(?:Video|Видео|Text|Текст|Subtitle|Субтитры|Menu|Меню)(?:\s*#\d+)?",
            stripped,
            re.I,
        ):
            section = None
            continue
        if section == "audio":
            match = re.match(r"(?:Language|Язык)\s*:\s*(.+)", stripped, re.I)
            if match:
                values = extract_languages(match[1])
                current.update(values)
                if values:
                    excerpts.append(stripped)
    if current:
        dumps.append(current)
    if not dumps:
        return []

    common = set.intersection(*dumps) if len(dumps) > 1 else set()
    evidence = []
    if common:
        evidence.append(
            Evidence(
                field="audio_languages",
                value=sorted(common),
                source="description",
                excerpt="; ".join(excerpts)[:300],
                scope="all_video_files",
                delivery="embedded",
            )
        )
    remaining = set.union(*dumps) - common
    if remaining or len(dumps) == 1:
        values = remaining or dumps[0]
        evidence.append(
            Evidence(
                field="audio_languages",
                value=sorted(values),
                source="description",
                excerpt="; ".join(excerpts)[:300],
                scope="release",
                delivery="embedded",
            )
        )
    return evidence


def description_evidence(text):
    evidence = mediainfo_audio_evidence(text)
    section = None
    section_lines = []

    def flush():
        if not section:
            return
        excerpt = " ".join(section_lines)
        values = extract_languages(excerpt)
        technical = bool(
            re.search(
                r"\b(?:AAC|AC3|EAC3|FLAC|DTS|TrueHD|MLP|PCM|Opus|Vorbis|MP3|ASS|SSA|SRT|PGS)\b", excerpt, re.I
            )
        )
        external = bool(re.search(r"внешн|external", excerpt, re.I))
        embedded = bool(re.search(r"в составе контейнера|embedded|встроенн", excerpt, re.I))
        delivery = "external" if external else "embedded" if embedded or technical else "unspecified"
        # Technical track specifications describe the release layout. Generic language
        # lists and file-specific MediaInfo dumps cannot establish batch coverage.
        scoped = technical and not re.search(
            r"(?:только|only)\s+(?:для\s+)?(?:сер|эпиз|ep)|S\d+E\d+", excerpt, re.I
        )
        if values:
            evidence.append(
                Evidence(
                    field=section,
                    value=values,
                    source="description",
                    excerpt=excerpt[:300],
                    scope="all_video_files" if scoped else "release",
                    delivery=delivery,
                )
            )

    # Keep broad release claims broad: a batch's languages are not proof for every file.
    for line in text.splitlines():
        lower = line.strip().lower()
        if re.match(r"(?:mediainfo|общее|general|полное имя|complete name)\b", lower):
            flush()
            section = None
            break
        kind = (
            "audio_languages"
            if re.match(r"(?:аудио|audio|язык аудио)\b", lower)
            else ("subtitle_languages" if re.match(r"(?:субтитры|subtitles|subs)\b", lower) else None)
        )
        if kind:
            flush()
            section, section_lines = kind, [line]
        elif section and (
            not lower
            or re.match(r"#\d+|переводчик|редактор", lower)
            or re.match(
                r"(?:видео|video|контейнер|формат|качество|размер|duration|раздача|описание|скриншоты)\b",
                lower,
            )
        ):
            flush()
            section, section_lines = None, []
        elif section:
            if extract_languages(line) and extract_languages(" ".join(section_lines)):
                flush()
                section_lines = []
            section_lines.append(line)
            if len(section_lines) >= 8:
                flush()
                section, section_lines = None, []
    flush()
    return evidence


def title_subtitle_evidence(text):
    """Extract explicit compact claims such as ``rus Sub`` from release titles."""
    values = []
    excerpts = []
    for clause in re.split(r"\s*(?:\+|\||;)\s*", text):
        if not re.search(r"\b(?:sub(?:title)?s?|субтитр\w*)\b", clause, re.I):
            continue
        languages = extract_languages(clause)
        if languages:
            values.extend(languages)
            excerpts.append(clause.strip())
    values = list(dict.fromkeys(values))
    if not values:
        return []
    return [
        Evidence(
            field="subtitle_languages",
            value=values,
            source="title",
            excerpt=" + ".join(excerpts)[:300],
            scope="release",
            delivery="external",
        )
    ]


def _title_key(value):
    return re.sub(r"[^\w]", "", value.casefold())


def search_title(media):
    """Preserve the principal spelling; alias order is not a language preference."""
    original = media.original_title or media.title
    if not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", original):
        return original
    # Localized titles sometimes retain a Latin brand before their translation.
    # Only use that prefix if metadata also lists it as a complete title.
    prefix = re.match(r"[A-Za-z0-9][A-Za-z0-9: .!?'_-]*", media.title)
    if prefix:
        anchor = prefix[0].strip(" ._-")
        if len(re.findall(r"[A-Za-z]", anchor)) >= 4:
            equivalent = [name for name in media.aliases if _title_key(name) == _title_key(anchor)]
            if equivalent:
                exact = [name for name in equivalent if name.casefold() == anchor.casefold()]
                return sorted(exact or equivalent, key=lambda name: (name.casefold(), name))[0]
    # Do not silently substitute an arbitrary foreign translation from aliases.
    return media.title or original


def search_titles(media):
    primary = search_title(media)
    variants = [name for name in media.aliases if _title_key(name) == _title_key(primary)]
    result = [primary]
    seen = {primary.casefold()}
    for name in sorted(variants, key=lambda name: (" " not in name, name.casefold(), name)):
        if name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    return result[:4]
