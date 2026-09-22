"""Stable, dependency-free plugin contract (providers use libraries supplied by Lazarr)."""

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse
import httpx
from pydantic import BaseModel, Field, field_validator
from lazarr.config import Requirements
from lazarr.languages import language as language, ALIASES

LANGUAGES = ALIASES

SDK_VERSION = "1.6"


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(p in {"..", "."} for p in path.parts):
        raise ValueError("Unsafe torrent path")
    if ":" in value or "\x00" in value:
        raise ValueError("Unsafe torrent path")
    return str(path)


class ProviderError(Exception):
    def __init__(self, code: str, message: str = "Provider request failed", retry_after: int = 0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class ConfigField(BaseModel):
    name: str
    label: str
    secret: bool = False
    required: bool = False
    default: str = ""


class ProviderManifest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str
    kind: Literal["metadata", "content", "calendar"]
    version: str
    sdk: str = ">=1,<2"
    config_fields: list[ConfigField] = Field(default_factory=list)
    auth_methods: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class AuthResult(BaseModel):
    status: Literal["authenticated", "anonymous", "required", "challenge"]
    message: str = ""
    fields: list[ConfigField] = Field(default_factory=list)
    image_url: str | None = None


class MetadataItem(BaseModel):
    provider: str = "tmdb"
    id: str
    kind: Literal["movie", "tv"]
    title: str
    original_title: str = ""
    year: int | None = None
    genre_ids: list[int] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    origin_countries: list[str] = Field(default_factory=list)
    original_language: str = ""
    taxonomy_known: bool = False
    overview: str = ""
    poster: str | None = None
    backdrop: str | None = None
    aliases: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    release_date: str | None = None
    seasons: list[dict] = Field(default_factory=list)
    community_rating: float | None = None
    official_rating: str | None = None
    status: str | None = None
    studios: list[str] = Field(default_factory=list)
    people: list[dict] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    remote_trailers: list[dict] = Field(default_factory=list)
    collection: str | None = None
    # Explicit provider mappings, keyed by canonical "season:episode".
    episode_numbering: dict[str, list[dict]] = Field(default_factory=dict)


class EpisodeInfo(BaseModel):
    id: str
    number: int
    title: str = ""
    overview: str = ""
    still: str | None = None
    air_date: str | None = None
    absolute_number: int | None = None


class SeasonInfo(BaseModel):
    number: int
    title: str = ""
    episodes: list[EpisodeInfo]


class SearchQuery(BaseModel):
    text: str = ""
    media: MetadataItem
    season: int | None = None
    episodes: list[int] = Field(default_factory=list)
    requirements: Requirements


class Evidence(BaseModel):
    field: str
    value: Any
    source: Literal["title", "description", "filename", "structured", "probe", "torrent"]
    excerpt: str = ""
    file_path: str | None = None
    scope: Literal["release", "file", "all_video_files"] = "release"
    complete: bool = False
    delivery: Literal["embedded", "external", "unspecified"] = "unspecified"


class TorrentFile(BaseModel):
    index: int
    path: str
    size: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)

    @field_validator("path")
    @classmethod
    def path_safe(cls, value):
        return safe_relative_path(value)


class Candidate(BaseModel):
    provider: str
    id: str
    revision: str = ""
    url: str
    title: str
    description: str = ""
    size: int | None = None
    seeds: int | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    # Provider file lists are hints only. Worker replaces them with torrent metadata.
    file_hints: list[str] = Field(default_factory=list)
    download_url: str | None = None
    magnet: str | None = None


class SearchPage(BaseModel):
    items: list[Candidate]
    next_cursor: str | None = None


class DownloadSource(BaseModel):
    torrent: bytes | None = Field(default=None, exclude=True)
    magnet: str | None = None


class SubtaskRequest(BaseModel):
    id: int
    media: MetadataItem
    season: int | None = None
    episode: int | None = None
    absolute_number: int | None = None
    requirements: Requirements
    air_date: str | None = None


class MatchResult(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class Criterion(BaseModel):
    field: str
    result: MatchResult
    reason: str
    required: bool = True
    evidence: list[Evidence] = Field(default_factory=list)


class TrackBinding(BaseModel):
    language_source: Literal["filename", "title", "description", "probe", "content"] = "filename"
    kind: Literal["audio", "subtitle"]
    language: str = "und"
    file_index: int | None = None
    path: str | None = None
    embedded: bool = False
    title: str | None = None
    forced: bool = False


class FileBinding(BaseModel):
    subtask_id: int
    video_index: int
    video_path: str
    episode_order: int
    tracks: list[TrackBinding] = Field(default_factory=list)
    resolution: int | None = None
    missing_subtitle_languages: list[str] = Field(default_factory=list)


class SubtaskEvaluation(BaseModel):
    subtask_id: int
    result: MatchResult
    criteria: list[Criterion]
    binding: FileBinding | None = None


class DownloadPlan(BaseModel):
    infohash: str
    bindings: list[FileBinding]
    files: list[TorrentFile]


class EvaluationReport(BaseModel):
    evaluations: list[SubtaskEvaluation]
    plan: DownloadPlan | None = None
    phase: Literal["preflight", "verification"] = "preflight"


class ReleaseDate(BaseModel):
    value: str | None
    precision: Literal["date", "datetime", "unknown"] = "unknown"
    source: str = "tmdb"


class ProviderContext:
    def __init__(self, config: dict, state: dict, client: httpx.AsyncClient):
        self.config, self.state, self.http = config, state, client
        self.solver_transport = None
        self.request_gate = None
        if state.get("browser_user_agent"):
            self.http.headers["User-Agent"] = state["browser_user_agent"]

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        from lazarr.cloudflare import challenged, solve
        from lazarr.rate_limit import retry_after

        browser_html = kwargs.pop("browser_html", False)
        form_encoding = kwargs.pop("browser_form_encoding", "utf-8")
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", "Network request failed", 60) from exc
        if response.status_code == 429:
            raise ProviderError(
                "rate_limited",
                "Провайдер ограничил частоту запросов (HTTP 429)",
                retry_after(response.headers.get("retry-after")),
            )
        if challenged(response):
            response = await solve(self, method, url, kwargs, html=browser_html, form_encoding=form_encoding)
        if response.status_code == 429:
            raise ProviderError(
                "rate_limited",
                "Провайдер ограничил частоту запросов (HTTP 429)",
                retry_after(response.headers.get("retry-after")),
            )
        if response.status_code in {401, 403}:
            raise ProviderError("auth_required", "Provider requires authentication")
        if response.status_code == 404:
            raise ProviderError("not_found", "Resource no longer exists")
        if response.is_error:
            raise ProviderError(
                "unavailable",
                f"Provider HTTP {response.status_code}",
                retry_after(response.headers.get("retry-after")),
            )
        return response

    def base_url(self, default: str) -> str:
        result = self.config.get("base_url") or default
        parsed = urlparse(result)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ProviderError("configuration", "Invalid provider base URL")
        return result.rstrip("/")


class Provider(ABC):
    manifest: ProviderManifest

    def __init__(self, context: ProviderContext):
        self.ctx = context

    async def authenticate(self, values: dict) -> AuthResult:
        return AuthResult(status="anonymous")

    async def auth_status(self) -> AuthResult:
        return AuthResult(status="anonymous")

    async def logout(self):
        self.ctx.state.clear()
        self.ctx.http.cookies.clear()

    async def healthcheck(self) -> dict:
        return {"ok": True}

    async def close(self):
        pass


class MetadataProvider(Provider):
    @abstractmethod
    async def search(self, query: str) -> list[MetadataItem]: ...
    @abstractmethod
    async def get_media(self, kind: str, media_id: str) -> MetadataItem: ...
    @abstractmethod
    async def get_season(self, media_id: str, season: int) -> SeasonInfo: ...


class ContentProvider(Provider):
    @abstractmethod
    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage: ...
    @abstractmethod
    async def inspect(self, candidate: Candidate) -> Candidate: ...
    @abstractmethod
    async def resolve_download(self, candidate: Candidate) -> DownloadSource: ...


class ReleaseCalendarProvider(ABC):
    @abstractmethod
    async def release_date(self, media: MetadataItem, episode: EpisodeInfo | None) -> ReleaseDate: ...
