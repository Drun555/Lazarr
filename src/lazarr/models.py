import time
from typing import Any
from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(default=time.time)


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    csrf: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[float] = mapped_column(Float)


class ConfigEntry(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(default="")
    username: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="pending")
    requested_at: Mapped[float] = mapped_column(default=time.time)
    reply: Mapped[str] = mapped_column(Text, default="")
    reply_version: Mapped[int] = mapped_column(default=0)
    retry_at: Mapped[float] = mapped_column(default=0)
    delivery_error: Mapped[str] = mapped_column(default="")
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    dialog: Mapped[dict] = mapped_column(JSON, default=dict)
    inbox: Mapped[list] = mapped_column(JSON, default=list)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    id: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets: Mapped[str] = mapped_column(Text, default="")
    session_state: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_at: Mapped[float] = mapped_column(default=0.0)


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (UniqueConstraint("provider", "external_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(default="tmdb")
    external_id: Mapped[str]
    kind: Mapped[str]
    title: Mapped[str]
    year: Mapped[int | None] = mapped_column(nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("media_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    number: Mapped[int]
    title: Mapped[str] = mapped_column(default="")
    refreshed_at: Mapped[float] = mapped_column(default=time.time)


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("season_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    number: Mapped[int]
    external_id: Mapped[str | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    still: Mapped[str | None] = mapped_column(Text, nullable=True)
    air_date: Mapped[str | None] = mapped_column(nullable=True)
    absolute_number: Mapped[int | None] = mapped_column(nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("uq_tasks_media_id", "media_id", unique=True),)
    numbering: Mapped[dict] = mapped_column(JSON, default=dict)
    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    requirements: Mapped[dict] = mapped_column(JSON)
    whole_season: Mapped[bool] = mapped_column(default=True)
    paused: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[float] = mapped_column(default=time.time)
    updated_at: Mapped[float] = mapped_column(default=time.time)


class TaskSeason(Base):
    __tablename__ = "task_seasons"
    __table_args__ = (UniqueConstraint("task_id", "selection_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    selection_key: Mapped[str]
    whole_season: Mapped[bool] = mapped_column(default=True)
    numbering: Mapped[dict] = mapped_column(JSON, default=dict)


class Subtask(Base):
    __tablename__ = "subtasks"
    __table_args__ = (UniqueConstraint("task_id", "part_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)
    part_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued")
    last_search_at: Mapped[float | None] = mapped_column(nullable=True)
    next_search_at: Mapped[float] = mapped_column(default=0.0)
    selection_hidden_until: Mapped[float] = mapped_column(default=0.0, server_default="0")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_subtitle_languages: Mapped[list] = mapped_column(JSON, default=list)


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("provider", "external_id", "revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str]
    external_id: Mapped[str]
    revision: Mapped[str] = mapped_column(default="")
    data: Mapped[dict] = mapped_column(JSON)


class Download(Base):
    __tablename__ = "downloads"
    id: Mapped[int] = mapped_column(primary_key=True)
    infohash: Mapped[str] = mapped_column(unique=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"))
    save_path: Mapped[str] = mapped_column(Text)
    torrent_file: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(default="starting")
    manual_paused: Mapped[bool] = mapped_column(default=False)
    plan: Mapped[dict] = mapped_column(JSON)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded: Mapped[int] = mapped_column(default=0)
    downloaded: Mapped[int] = mapped_column(default=0)
    seed_ratio: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[float] = mapped_column(default=time.time)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("download_id", "video_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    download_id: Mapped[int] = mapped_column(ForeignKey("downloads.id"))
    video_index: Mapped[int]
    path: Mapped[str] = mapped_column(Text)
    tracks: Mapped[list] = mapped_column(JSON, default=list)
    resolution: Mapped[int | None] = mapped_column(nullable=True)
    probe: Mapped[dict] = mapped_column(JSON, default=dict)


class SubtaskAsset(Base):
    __tablename__ = "subtask_assets"
    __table_args__ = (UniqueConstraint("subtask_id", "asset_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    subtask_id: Mapped[int] = mapped_column(ForeignKey("subtasks.id"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"))
    current: Mapped[bool] = mapped_column(default=False)
    pending: Mapped[bool] = mapped_column(default=True)
    preflight: Mapped[dict] = mapped_column(JSON)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    override: Mapped[bool] = mapped_column(default=False)


class LibraryAsset(Base):
    """A verified media file retained independently from the task that found it."""

    __tablename__ = "library_assets"
    __table_args__ = (UniqueConstraint("media_id", "part_key", "asset_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id"))
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id"), nullable=True)
    part_key: Mapped[str] = mapped_column(String(80))
    asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"))
    preflight: Mapped[dict] = mapped_column(JSON, default=dict)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)


class CandidateDecision(Base):
    __tablename__ = "candidate_decisions"
    __table_args__ = (UniqueConstraint("subtask_id", "release_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    subtask_id: Mapped[int] = mapped_column(ForeignKey("subtasks.id"))
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"))
    report: Mapped[dict] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(default="evaluated")
    updated_at: Mapped[float] = mapped_column(default=time.time)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str]
    target: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)


class PlaybackProgress(Base):
    __tablename__ = "playback_progress"
    __table_args__ = (UniqueConstraint("user_id", "item_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    item_id: Mapped[str] = mapped_column(String(36))
    position_ticks: Mapped[int] = mapped_column(default=0)
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int] = mapped_column(default=0)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    likes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_played_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(default=time.time)


class PlaybackSession(Base):
    """Durable playback history; session keys are scoped to an account and item."""

    __tablename__ = "playback_sessions"
    __table_args__ = (UniqueConstraint("user_id", "item_id", "session_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    session_key: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[str] = mapped_column(String(36))
    part_key: Mapped[str] = mapped_column(String(80), default="")
    position_ticks: Mapped[int] = mapped_column(default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[float] = mapped_column(default=time.time)
    updated_at: Mapped[float] = mapped_column(default=time.time)
    stopped_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class VideoPlaylist(Base):
    __tablename__ = "video_playlists"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(256))
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stable entry IDs preserve duplicate videos and survive reorder operations.
    entries: Mapped[list] = mapped_column(JSON, default=list)
    shares: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)
