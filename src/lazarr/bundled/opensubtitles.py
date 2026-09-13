"""OpenSubtitles REST API adapter."""

from pathlib import PurePath

from lazarr.sdk import (
    AuthResult,
    ConfigField,
    ProviderError,
    ProviderManifest,
    SubtitleCandidate,
    SubtitleFile,
    SubtitleProvider,
)


class Plugin(SubtitleProvider):
    manifest = ProviderManifest(
        id="opensubtitles",
        name="OpenSubtitles",
        kind="subtitle",
        version="1.0.1",
        sdk=">=1.5,<2",
        config_fields=[
            ConfigField(name="api_key", label="API key", secret=True, required=True),
            ConfigField(name="base_url", label="API URL", default="https://api.opensubtitles.com/api/v1"),
        ],
        auth_methods=["api_key"],
        capabilities=["movies", "episodes", "external_subtitles"],
    )

    def headers(self):
        key = self.ctx.config.get("api_key", "").strip()
        if not key:
            raise ProviderError("configuration", "Укажите OpenSubtitles API key в настройках")
        return {"Api-Key": key, "User-Agent": "Lazarr v0.1"}

    async def search(self, query):
        params = {"languages": ",".join(query.languages), "order_by": "download_count"}
        tmdb = query.external_ids.get("tmdb", "")
        imdb = query.external_ids.get("imdb", "").removeprefix("tt")
        if tmdb.isdigit():
            params["tmdb_id"] = tmdb
        elif imdb.isdigit():
            params["imdb_id"] = imdb
        else:
            params["query"] = query.title
        if query.media_kind == "episode":
            params["type"] = "episode"
            if query.season is not None:
                params["season_number"] = query.season
            if query.episode is not None:
                params["episode_number"] = query.episode
        else:
            params["type"] = "movie"
            if query.year:
                params["year"] = query.year
        response = await self.ctx.request(
            "GET",
            self.ctx.base_url("https://api.opensubtitles.com/api/v1") + "/subtitles",
            params=params,
            headers=self.headers(),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("parse_error", "OpenSubtitles вернул некорректный JSON") from exc
        result = []
        for item in payload.get("data", []):
            attributes = item.get("attributes") or {}
            language = attributes.get("language") or "und"
            for file in attributes.get("files") or []:
                file_id = file.get("file_id")
                if not file_id:
                    continue
                result.append(
                    SubtitleCandidate(
                        id=str(file_id),
                        language=language,
                        filename=file.get("file_name") or f"subtitle-{file_id}.srt",
                        release=attributes.get("release") or "",
                        downloads=attributes.get("download_count") or 0,
                        rating=attributes.get("ratings") or 0,
                        hearing_impaired=bool(attributes.get("hearing_impaired")),
                        machine_translated=bool(attributes.get("machine_translated")),
                        ai_translated=bool(attributes.get("ai_translated")),
                    )
                )
        return result

    async def download(self, candidate):
        response = await self.ctx.request(
            "POST",
            self.ctx.base_url("https://api.opensubtitles.com/api/v1") + "/download",
            json={"file_id": int(candidate.id)},
            headers=self.headers(),
        )
        try:
            payload = response.json()
            link = payload["link"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("parse_error", "OpenSubtitles не вернул ссылку на файл") from exc
        file_response = await self.ctx.request("GET", link, headers={"User-Agent": "Lazarr v0.1"})
        if not file_response.content:
            raise ProviderError("invalid_file", "OpenSubtitles вернул пустой файл")
        if len(file_response.content) > 10 * 1024 * 1024:
            raise ProviderError("invalid_file", "Файл субтитров превышает 10 MiB")
        if file_response.content.startswith((b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf\x27\x1c")):
            raise ProviderError("invalid_file", "Архивы субтитров не поддерживаются")
        filename = payload.get("file_name") or candidate.filename
        suffix = PurePath(filename).suffix.lower()
        if suffix not in {".srt", ".ass", ".ssa", ".vtt", ".sub"}:
            raise ProviderError("invalid_file", "OpenSubtitles вернул неподдерживаемый формат")
        return SubtitleFile(content=file_response.content, filename=filename)

    async def auth_status(self):
        return AuthResult(status="authenticated" if self.ctx.config.get("api_key") else "required")

    async def healthcheck(self):
        response = await self.ctx.request(
            "GET",
            self.ctx.base_url("https://api.opensubtitles.com/api/v1") + "/infos/languages",
            headers=self.headers(),
        )
        return {"ok": response.status_code == 200}
