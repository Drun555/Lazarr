"""TMDB metadata adapter; all runtime dependencies are supplied by Lazarr."""

import re
from lazarr.sdk import (
    MetadataProvider,
    ProviderManifest,
    ConfigField,
    MetadataItem,
    SeasonInfo,
    EpisodeInfo,
    ProviderError,
    AuthResult,
)


class Plugin(MetadataProvider):
    manifest = ProviderManifest(
        id="tmdb",
        name="TMDB",
        kind="metadata",
        version="1.0.3",
        sdk=">=1.4,<2",
        config_fields=[
            ConfigField(name="api_key", label="API key или Read Access Token", secret=True, required=True),
            ConfigField(name="base_url", label="API URL", default="https://api.themoviedb.org/3"),
            ConfigField(
                name="image_base_url", label="URL изображений", default="https://image.tmdb.org/t/p/w342"
            ),
            ConfigField(name="language", label="Язык метаданных", default="ru-RU"),
        ],
        auth_methods=["api_key", "bearer"],
        capabilities=["movies", "series", "seasons", "release_dates"],
    )

    async def get(self, path, **params):
        key = self.ctx.config.get("api_key", "")
        if not key:
            raise ProviderError("configuration", "Укажите TMDB API key в настройках")
        headers = {}
        if len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key):
            params["api_key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
        params["language"] = self.ctx.config.get("language", "ru-RU")
        response = await self.ctx.request(
            "GET", self.ctx.base_url("https://api.themoviedb.org/3") + path, params=params, headers=headers
        )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("parse_error", "TMDB returned invalid JSON") from exc

    def item(self, data, kind):
        title = data.get("title") if kind == "movie" else data.get("name")
        original = data.get("original_title") if kind == "movie" else data.get("original_name")
        date = data.get("release_date") if kind == "movie" else data.get("first_air_date")
        alternatives = data.get("alternative_titles", {})
        names = alternatives.get("titles", alternatives.get("results", []))
        ids = {"tmdb": str(data["id"])}
        ids.update(
            {
                k.removesuffix("_id"): str(v)
                for k, v in data.get("external_ids", {}).items()
                if k in {"imdb_id", "tvdb_id"} and v
            }
        )
        return MetadataItem(
            id=str(data["id"]),
            kind=kind,
            title=title or original or "Без названия",
            original_title=original or "",
            year=int(date[:4]) if date else None,
            genre_ids=data.get("genre_ids", [g["id"] for g in data.get("genres", [])]),
            genres=[g["name"] for g in data.get("genres", [])],
            origin_countries=data.get(
                "origin_country", [c["iso_3166_1"] for c in data.get("production_countries", [])]
            ),
            original_language=data.get("original_language", ""),
            taxonomy_known="genres" in data or "genre_ids" in data,
            overview=data.get("overview", ""),
            release_date=date or None,
            poster=f"https://image.tmdb.org/t/p/w342{data['poster_path']}"
            if data.get("poster_path")
            else None,
            aliases=list(dict.fromkeys([original or ""] + [n["title"] for n in names])),
            external_ids=ids,
            seasons=[
                {"number": s["season_number"], "title": s["name"], "episode_count": s["episode_count"]}
                for s in data.get("seasons", [])
            ],
        )

    async def search(self, query):
        data = await self.get("/search/multi", query=query, include_adult="false")
        return [
            self.item(item, item["media_type"])
            for item in data.get("results", [])
            if item.get("media_type") in {"movie", "tv"}
        ]

    async def get_media(self, kind, media_id):
        if kind not in {"movie", "tv"} or not str(media_id).isdigit():
            raise ProviderError("configuration", "Invalid TMDB identity")
        data = await self.get(
            f"/{kind}/{media_id}",
            append_to_response="external_ids,alternative_titles,episode_groups"
            if kind == "tv"
            else "external_ids,alternative_titles",
        )
        item = self.item(data, kind)
        groups = [
            g
            for g in data.get("episode_groups", {}).get("results", [])
            if g.get("type") == 6 and g.get("name", "").casefold() == "seasons" and g.get("episode_count")
        ]
        # Only a uniquely named TV-season ordering is unambiguous. Other
        # group types (story arcs, director's cut) must not become aliases.
        if len(groups) == 1:
            try:
                ordering = await self.get(f"/tv/episode_group/{groups[0]['id']}")
                mapping = {}
                for group in ordering.get("groups", []):
                    match = re.fullmatch(r"Season (\d+)", group.get("name", ""), re.I)
                    if not match:
                        continue
                    season_number = int(match[1])
                    for episode in group.get("episodes", []):
                        key = f"{episode['season_number']}:{episode['episode_number']}"
                        mapping.setdefault(key, []).append(
                            {
                                "season": season_number,
                                "episode": episode["order"] + 1,
                                "source": f"tmdb:episode_group:{groups[0]['id']}",
                            }
                        )
                # Do not keep an alternative coordinate that names multiple canonical episodes.
                coordinates = {}
                for key, aliases in mapping.items():
                    for alias in aliases:
                        coordinates.setdefault((alias["season"], alias["episode"]), set()).add(key)
                item.episode_numbering = {
                    key: [a for a in aliases if len(coordinates[(a["season"], a["episode"])]) == 1]
                    for key, aliases in mapping.items()
                }
            except ProviderError:
                pass  # Main metadata remains usable when alternate ordering is unavailable.
        return item

    async def get_season(self, media_id, season):
        if not str(media_id).isdigit() or season < 0:
            raise ProviderError("configuration", "Invalid season")
        data = await self.get(f"/tv/{media_id}/season/{season}")
        return SeasonInfo(
            number=season,
            title=data.get("name", ""),
            episodes=[
                EpisodeInfo(
                    id=str(e["id"]),
                    number=e["episode_number"],
                    title=e.get("name", ""),
                    air_date=e.get("air_date"),
                )
                for e in data.get("episodes", [])
            ],
        )

    async def auth_status(self):
        return AuthResult(status="authenticated" if self.ctx.config.get("api_key") else "required")

    async def healthcheck(self):
        await self.get("/configuration")
        return {"ok": True}
