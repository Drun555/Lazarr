"""Podnapisi anonymous JSON API adapter."""

from io import BytesIO
from pathlib import PurePath
import re
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from lazarr.sdk import (
    AuthResult,
    ConfigField,
    ProviderError,
    ProviderManifest,
    SubtitleCandidate,
    SubtitleFile,
    SubtitleProvider,
    language,
)


SUPPORTED_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024
MAX_PAGES = 10


class Plugin(SubtitleProvider):
    manifest = ProviderManifest(
        id="podnapisi",
        name="Podnapisi",
        kind="subtitle",
        version="1.0.1",
        sdk=">=1.5,<2",
        config_fields=[
            ConfigField(name="base_url", label="URL сайта", default="https://www.podnapisi.net"),
        ],
        auth_methods=[],
        capabilities=["movies", "episodes", "external_subtitles", "anonymous", "legacy_tls"],
    )

    def base_url(self):
        return self.ctx.base_url("https://www.podnapisi.net")

    @staticmethod
    def _titles(query):
        alternatives = list(dict.fromkeys(value.strip() for value in query.aliases if value.strip()))
        alternatives.sort(
            key=lambda value: (
                not bool(re.search(r"[a-z]", value, re.I)),
                len(re.findall(r"[^\x00-\x7f]", value)),
                len(value),
            )
        )
        return list(dict.fromkeys([query.title.strip(), *alternatives]))[:8]

    @staticmethod
    def _candidate(item, wanted, query):
        identity = str(item.get("id") or "").strip()
        if not identity or language(item.get("language")) != wanted:
            return None
        movie = item.get("movie") or {}
        episode_info = movie.get("episode_info") or {}
        if query.media_kind == "episode":
            if movie.get("type") == "movie":
                return None
            if query.season is not None and episode_info.get("season") not in {None, query.season}:
                return None
            if query.episode is not None and episode_info.get("episode") not in {None, query.episode}:
                return None
        elif movie.get("type") not in {None, "movie"}:
            return None
        releases = [
            str(value) for value in [*(item.get("releases") or []), *(item.get("custom_releases") or [])]
        ]
        stats = item.get("stats") or {}
        flags = item.get("flags") or []
        filename = releases[0] if releases else f"{identity}.srt"
        if PurePath(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
            filename += ".srt"
        return SubtitleCandidate(
            id=identity,
            language=wanted,
            filename=filename,
            release=" ".join(releases),
            downloads=int(stats.get("downloads") or 0),
            hearing_impaired="hearing_impaired" in flags,
        )

    async def _search_title(self, query, title, wanted):
        params = {"keywords": title, "language": wanted}
        if query.media_kind == "episode":
            params["movie_type"] = ["tv-series", "mini-series"]
            if query.season is not None:
                params["seasons"] = query.season
            if query.episode is not None:
                params["episodes"] = query.episode
        else:
            params["movie_type"] = "movie"
        if query.year:
            params["year"] = query.year

        results = []
        page = 1
        while page <= MAX_PAGES:
            request_params = {**params, "page": page} if page > 1 else params
            response = await self.ctx.request(
                "GET",
                self.base_url() + "/subtitles/search/advanced",
                params=request_params,
                headers={"Accept": "application/json"},
            )
            try:
                payload = response.json()
                rows = payload["data"]
                current = int(payload.get("page") or page)
                pages = int(payload.get("all_pages") or current)
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError("parse_error", "Podnapisi вернул некорректный ответ") from exc
            for row in rows:
                candidate = self._candidate(row, wanted, query)
                if candidate:
                    results.append(candidate)
            if current >= pages:
                break
            page = current + 1
        return results

    async def search(self, query):
        results = []
        seen = set()
        for requested in query.languages:
            wanted = language(requested)
            if wanted == "und":
                continue
            for title in self._titles(query):
                found = await self._search_title(query, title, wanted)
                for candidate in found:
                    if candidate.id not in seen:
                        seen.add(candidate.id)
                        results.append(candidate)
                if found:
                    break
        return results

    async def download(self, candidate):
        response = await self.ctx.request(
            "GET",
            self.base_url() + f"/subtitles/{quote(candidate.id, safe='')}/download",
            params={"container": "zip"},
        )
        content = response.content
        if not content or len(content) > MAX_DOWNLOAD_SIZE:
            raise ProviderError("invalid_file", "Podnapisi вернул пустой или слишком большой файл")
        if not content.startswith(b"PK\x03\x04"):
            suffix = PurePath(candidate.filename).suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                raise ProviderError("invalid_file", "Podnapisi вернул неподдерживаемый формат")
            return SubtitleFile(content=content, filename=candidate.filename)
        try:
            with ZipFile(BytesIO(content)) as archive:
                members = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir() and PurePath(item.filename).suffix.lower() in SUPPORTED_SUFFIXES
                ]
                if len(members) != 1:
                    raise ProviderError(
                        "invalid_file", "Архив Podnapisi должен содержать ровно один файл субтитров"
                    )
                member = members[0]
                if member.file_size > MAX_DOWNLOAD_SIZE or member.flag_bits & 0x1:
                    raise ProviderError("invalid_file", "Архив Podnapisi слишком большой или зашифрован")
                extracted = archive.read(member)
        except BadZipFile as exc:
            raise ProviderError("invalid_file", "Podnapisi вернул повреждённый ZIP-архив") from exc
        if not extracted:
            raise ProviderError("invalid_file", "Podnapisi вернул пустые субтитры")
        return SubtitleFile(content=extracted, filename=PurePath(member.filename).name)

    async def auth_status(self):
        return AuthResult(status="anonymous", message="Авторизация не требуется")

    async def healthcheck(self):
        response = await self.ctx.request(
            "GET",
            self.base_url() + "/subtitles/search/advanced",
            params={"keywords": "test", "language": "en", "movie_type": "movie"},
            headers={"Accept": "application/json"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("parse_error", "Podnapisi вернул некорректный ответ") from exc
        return {"ok": isinstance(payload.get("data"), list)}
