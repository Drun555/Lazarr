from pathlib import Path
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import os


class Requirements(BaseModel):
    audio_languages: list[str] = Field(default_factory=lambda: ["ru"])
    subtitle_languages: list[str] = Field(default_factory=list)
    min_resolution: int = 720
    max_resolution: int = 1080
    keyword: str = Field(default="", max_length=200)

    @field_validator("audio_languages", "subtitle_languages")
    @classmethod
    def languages(cls, values):
        from lazarr.languages import language

        result = list(dict.fromkeys(language(v) for v in values))
        if any(v == "und" for v in result):
            raise ValueError("Неизвестный язык. Используйте название или код: Русский, ru, rus, russian")
        return result

    @model_validator(mode="after")
    def resolutions(self):
        allowed = {480, 576, 720, 1080, 1440, 2160, 4320}
        if self.min_resolution not in allowed or self.max_resolution not in allowed:
            raise ValueError("Unsupported resolution")
        if self.min_resolution > self.max_resolution:
            raise ValueError("Minimum resolution exceeds maximum")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(validate_default=True)
    defaults: Requirements = Field(default_factory=Requirements)
    movie_path: str = Field(default_factory=lambda: os.getenv("LAZARR_MOVIE_PATH", "downloads/movies"))
    series_path: str = Field(default_factory=lambda: os.getenv("LAZARR_SERIES_PATH", "downloads/series"))
    search_start: str = "00:00"
    seed_ratio: float | None = Field(default=1.0, ge=0, le=10000)
    plugin_repository: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_schedule(cls, values):
        values = dict(values)
        if "search_start" not in values and "window_start" in values:
            values["search_start"] = values["window_start"]
        return values

    @property
    def timezone(self):
        return host_timezone()

    @field_validator("search_start")
    @classmethod
    def valid_time(cls, value):
        from datetime import time

        time.fromisoformat(value)
        if len(value) != 5:
            raise ValueError("Use HH:MM")
        return value

    @field_validator("plugin_repository")
    @classmethod
    def valid_repository(cls, value):
        if value and not value.startswith("https://"):
            raise ValueError("Plugin repository must use HTTPS")
        return value

    @field_validator("movie_path", "series_path")
    @classmethod
    def valid_path(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Invalid download path")
        return str(Path(value).expanduser().absolute())


class RuntimeConfig:
    def __init__(self, data_dir: str | Path | None = None, background: bool = True):
        self.data_dir = Path(data_dir or os.getenv("LAZARR_DATA_DIR", "data")).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.plugin_dir = Path(os.getenv("LAZARR_PLUGIN_DIR", str(self.data_dir / "plugins")))
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self.background = background
        self.secure_cookie = os.getenv("LAZARR_SECURE_COOKIE", "0") == "1"
        self.ffprobe = os.getenv("LAZARR_FFPROBE", "ffprobe")
        self.listen_interfaces = os.getenv("LAZARR_TORRENT_LISTEN", "0.0.0.0:6881,[::]:6881")


def host_timezone():
    name = os.getenv("TZ", "").lstrip(":")
    if name:
        if name.startswith("/"):
            with open(name, "rb") as stream:
                return ZoneInfo.from_file(stream)
        return ZoneInfo(name)
    localtime = Path("/etc/localtime")
    if localtime.exists():
        with localtime.open("rb") as stream:
            return ZoneInfo.from_file(stream)
    return ZoneInfo("UTC")
