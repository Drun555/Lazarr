"""Read-only video discovery and common Jellyfin query options."""

import math
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from lazarr.jellyfin_state import bool_parameter, csv_parameter, number_parameter, page, parameter
from lazarr.languages import language, language_name
from lazarr.models import LibraryAsset, Task


def named_id(kind, name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lazarr:{kind}:{str(name).casefold()}"))


def metadata_fields(ctx, media, batch=None):
    from lazarr.jellyfin import library_ids
    from lazarr.library import library_kind

    data = media.metadata_json
    if batch is not None:
        cached = batch.media_metadata(media.id)
        added, extras = cached["added"], cached["extras"]
    else:
        with ctx.db.session() as db:
            added = db.scalar(
                select(LibraryAsset.created_at)
                .where(LibraryAsset.media_id == media.id)
                .order_by(LibraryAsset.created_at)
            )
            added = added or db.scalar(select(Task.created_at).where(Task.media_id == media.id))
            extras = list(
                db.execute(
                    select(LibraryAsset.part_key, LibraryAsset.asset_id).where(
                        LibraryAsset.media_id == media.id
                    )
                )
            )
    from lazarr.jellyfin import object_id
    from lazarr.jellyfin_resources import EXTRA_KINDS

    return {
        "ParentId": library_ids(ctx)[library_kind(media)],
        "SortName": media.title.casefold(),
        "DateCreated": datetime.fromtimestamp(added or 0, timezone.utc).isoformat(),
        "CommunityRating": data.get("community_rating"),
        "OfficialRating": data.get("official_rating"),
        "Status": data.get("status"),
        "Tags": data.get("tags", []),
        "Genres": data.get("genres", []),
        "Studios": [{"Name": name, "Id": named_id("Studio", name)} for name in data.get("studios", [])],
        "GenreItems": [{"Name": name, "Id": named_id("Genre", name)} for name in data.get("genres", [])],
        "People": [
            {**person, "Id": named_id("Person", person["Name"])}
            for person in data.get("people", [])
            if person.get("Name")
        ],
        "RemoteTrailers": data.get("remote_trailers", []),
        "CollectionName": data.get("collection"),
        "LocalTrailerCount": sum(key.startswith("trailer:") for key, _ in extras),
        "SpecialFeatureCount": sum(
            key.split(":")[0] in EXTRA_KINDS - {"trailer", "theme", "intro"} for key, _ in extras
        ),
        "ThemeVideoIds": [
            object_id("asset", identity) for key, identity in extras if key.startswith("theme:")
        ],
    }


def values(params, key, pipe=False):
    if not pipe:
        return csv_parameter(params, key)
    pairs = params.multi_items() if hasattr(params, "multi_items") else params.items()
    return [
        v.strip() for k, raw in pairs if k.casefold() == key.casefold() for v in raw.split("|") if v.strip()
    ]


def timestamp(value, name):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Invalid {name}") from exc


def advanced_filter(items, params):
    """Apply filters before DTO projection; stable multi-column sorting."""
    now = datetime.now(timezone.utc).timestamp()
    arrays = {
        "genres": ("Genres", True),
        "tags": ("Tags", True),
        "years": ("ProductionYear", False),
        "officialRatings": ("OfficialRating", True),
        "videoTypes": ("VideoType", False),
        "locationTypes": ("LocationType", False),
        "seriesStatus": ("Status", False),
        "containers": ("Container", False),
    }
    ranges = {}
    for prefix in ("min", "max"):
        for field in ("Width", "Height", "CommunityRating", "CriticRating", "PremiereDate", "DateCreated"):
            key = prefix + field
            raw = parameter(params, key)
            if raw is not None:
                try:
                    value = timestamp(raw, key) if "Date" in field else float(raw)
                    if not math.isfinite(value):
                        raise ValueError
                except ValueError as exc:
                    raise HTTPException(400, f"Invalid {key}") from exc
                ranges[key] = (prefix, field, value)
    result = []
    for item in items:
        name = item.get("SortName", item["Name"]).casefold()
        if item["Id"] in csv_parameter(params, "excludeItemIds"):
            continue
        if item.get("LocationType") in csv_parameter(params, "excludeLocationTypes"):
            continue
        matches = True
        for key, (field, pipe) in arrays.items():
            wanted = {v.casefold() for v in values(params, key, pipe)}
            actual = item.get(field)
            actual = actual if isinstance(actual, list) else [actual]
            if wanted and not wanted.intersection(str(v).casefold() for v in actual if v is not None):
                matches = False
        for key, field in (
            ("studioIds", "Studios"),
            ("genreIds", "GenreItems"),
            ("personIds", "People"),
            ("studios", "Studios"),
        ):
            wanted = set(values(params, key, True))
            attribute = "Name" if key == "studios" else "Id"
            if wanted and not wanted.intersection(p.get(attribute) for p in item.get(field, [])):
                matches = False
        person = parameter(params, "person")
        roles = csv_parameter(params, "personTypes")
        if person and not any(
            p.get("Name", "").casefold() == person.casefold() and (not roles or p.get("Type") in roles)
            for p in item.get("People", [])
        ):
            matches = False
        for key, typ in (("audioLanguages", "Audio"), ("subtitleLanguages", "Subtitle")):
            wanted = {language(v) for v in csv_parameter(params, key)}
            if wanted and not wanted.intersection(
                language(s.get("Language")) for s in item.get("MediaStreams", []) if s["Type"] == typ
            ):
                matches = False
        for key, typ in (("videoCodecs", "Video"), ("audioCodecs", "Audio")):
            wanted = {v.casefold() for v in csv_parameter(params, key)}
            if wanted and not wanted.intersection(
                s.get("Codec", "").casefold() for s in item.get("MediaStreams", []) if s["Type"] == typ
            ):
                matches = False
        premiere = timestamp(item["PremiereDate"], "PremiereDate") if item.get("PremiereDate") else None
        checks = {
            "hasSubtitles": item.get("HasSubtitles", False),
            "hasOverview": bool(item.get("Overview")),
            "hasOfficialRating": bool(item.get("OfficialRating")),
            "hasParentalRating": bool(item.get("OfficialRating")),
            "isHd": (item.get("Height") or 0) >= 720,
            "is4K": (item.get("Width") or 0) >= 3840,
            "isMissing": item.get("Type") == "Episode" and not item.get("MediaSources"),
            "isUnaired": premiere is not None and premiere > now,
            "isFolder": item.get("IsFolder", False),
            "is3D": bool(item.get("Video3DFormat")),
            "isPlaceHolder": item.get("IsPlaceHolder", False),
            "isLocked": False,
            "hasTrailer": bool(item.get("RemoteTrailers") or item.get("LocalTrailerCount")),
            "hasSpecialFeature": bool(item.get("SpecialFeatureCount")),
            "hasThemeVideo": bool(item.get("ThemeVideoIds")),
        }
        for provider in ("Imdb", "Tmdb", "Tvdb"):
            checks[f"has{provider}Id"] = bool(item.get("ProviderIds", {}).get(provider))
        for key, value in checks.items():
            if parameter(params, key) is not None and bool(value) != bool_parameter(params, key):
                matches = False
        for key in ("indexNumber", "parentIndexNumber"):
            if parameter(params, key) is not None and item.get(key[0].upper() + key[1:]) != number_parameter(
                params, key
            ):
                matches = False
        for key, test in (
            ("nameStartsWith", lambda v: name.startswith(v)),
            ("nameStartsWithOrGreater", lambda v: name >= v),
            ("nameLessThan", lambda v: name < v),
        ):
            raw = parameter(params, key)
            if raw is not None and not test(raw.casefold()):
                matches = False
        image_types = csv_parameter(params, "imageTypes")
        if image_types and not any(
            t in item.get("ImageTags", {}) or (t == "Backdrop" and item.get("BackdropImageTags"))
            for t in image_types
        ):
            matches = False
        for key, (prefix, field, value) in ranges.items():
            actual = item.get(field)
            if actual is not None and "Date" in field:
                actual = timestamp(actual, key)
            if actual is None or (actual < value if prefix == "min" else actual > value):
                matches = False
        if matches:
            result.append(item)
    orders = csv_parameter(params, "sortOrder") or ["Ascending"]
    if any(v not in {"Ascending", "Descending"} for v in orders):
        raise HTTPException(400, "Invalid sortOrder")
    sort_fields = csv_parameter(params, "sortBy")
    for index, field in reversed(list(enumerate(sort_fields))):

        def sort_value(item):
            if field in {"DatePlayed", "PlayCount", "IsFavorite"}:
                value = item.get("UserData", {}).get({"DatePlayed": "LastPlayedDate"}.get(field, field))
            elif field == "Random":
                value = uuid.uuid4().hex
            else:
                value = item.get(
                    {
                        "DateLastContentAdded": "DateCreated",
                        "Runtime": "RunTimeTicks",
                        "SeriesSortName": "SeriesName",
                        "PremiereDate": "PremiereDate",
                    }.get(field, field)
                )
            if isinstance(value, str):
                value = value.casefold()
            return value is not None, value

        result.sort(key=sort_value, reverse=orders[min(index, len(orders) - 1)] == "Descending")
    adjacent = parameter(params, "adjacentTo")
    if adjacent:
        index = next((i for i, item in enumerate(result) if item["Id"] == adjacent), None)
        result = [] if index is None else result[max(0, index - 1) : index] + result[index + 1 : index + 2]
    return result


OPTIONAL_FIELDS = {
    "Overview",
    "Genres",
    "GenreItems",
    "Studios",
    "People",
    "ProviderIds",
    "Path",
    "MediaSources",
    "MediaStreams",
    "Chapters",
    "DateCreated",
    "SortName",
    "Tags",
    "RemoteTrailers",
    "Trickplay",
}


def project(item, params):
    result = dict(item)
    fields = set(csv_parameter(params, "fields"))
    if parameter(params, "fields") is not None:
        for field in OPTIONAL_FIELDS - fields:
            result.pop(field, None)
    if not bool_parameter(params, "enableUserData", True):
        result.pop("UserData", None)
    images = bool_parameter(params, "enableImages", True)
    kinds = csv_parameter(params, "enableImageTypes")
    limit = number_parameter(params, "imageTypeLimit")
    if not images or limit == 0:
        kinds = []
        for field in list(result):
            if "Image" in field and ("Tag" in field or "ItemId" in field):
                result.pop(field)
    elif kinds:
        result["ImageTags"] = {k: v for k, v in result.get("ImageTags", {}).items() if k in kinds}
        if "Backdrop" not in kinds:
            result.pop("BackdropImageTags", None)
            result.pop("ParentBackdropImageTags", None)
        if "Primary" not in kinds:
            result.pop("SeriesPrimaryImageTag", None)
    if limit is not None:
        for field in ("BackdropImageTags", "ParentBackdropImageTags"):
            if field in result:
                result[field] = result[field][:limit]
    return result


def entities(items, kind):
    groups = {}
    for item in items:
        if item.get("Type") not in {"Movie", "Series"}:
            continue
        names = {
            "Genre": item.get("Genres", []),
            "Studio": [v["Name"] for v in item.get("Studios", [])],
            "Person": [v["Name"] for v in item.get("People", [])],
            "Year": [str(item["ProductionYear"])] if item.get("ProductionYear") else [],
            "BoxSet": [item["CollectionName"]] if item.get("CollectionName") else [],
        }[kind]
        for name in set(names):
            identity = named_id(kind, name)
            row = groups.setdefault(
                identity,
                {
                    "Id": identity,
                    "Name": name,
                    "Type": kind,
                    "IsFolder": True,
                    "ChildCount": 0,
                    "MovieCount": 0,
                    "SeriesCount": 0,
                    "ImageTags": {},
                },
            )
            row["ChildCount"] += 1
            row[item["Type"] + "Count"] += 1
    return sorted(groups.values(), key=lambda r: r["Name"].casefold())


def install(app, context, authenticated, check_user, listing):
    from lazarr.jellyfin import item_dto, user_data
    from lazarr.jellyfin_state import filter_items

    def all_items(request, user):
        check_user(request, user)
        include_types = ",".join(csv_parameter(request.query_params, "includeItemTypes")) or None
        return listing(
            context(request),
            user,
            parameter(request.query_params, "parentId"),
            include_types=include_types,
            recursive=True,
        )

    @app.get("/Items/Counts")
    async def counts(request: Request, user=Depends(authenticated)):
        items = filter_items(all_items(request, user), request.query_params)
        return {
            typ + "Count": sum(i["Type"] == typ for i in items)
            for typ in ("Movie", "Series", "Episode", "Trailer", "BoxSet", "Program")
        }

    @app.get("/Items/Filters")
    @app.get("/Items/Filters2")
    async def filters(request: Request, user=Depends(authenticated)):
        items = filter_items(all_items(request, user), request.query_params)
        genres = sorted({g for i in items for g in i.get("Genres", [])})
        result = {"Genres": genres, "Tags": sorted({g for i in items for g in i.get("Tags", [])})}
        if request.url.path.endswith("Filters2"):
            result["Genres"] = [{"Name": g, "Id": named_id("Genre", g)} for g in genres]
            for typ in ("Audio", "Subtitle"):
                codes = sorted(
                    {s["Language"] for i in items for s in i.get("MediaStreams", []) if s["Type"] == typ}
                )
                result[typ + "Languages"] = [{"Name": language_name(v), "Value": v} for v in codes]
        else:
            result.update(
                Years=sorted({i["ProductionYear"] for i in items if i.get("ProductionYear")}),
                OfficialRatings=sorted({i["OfficialRating"] for i in items if i.get("OfficialRating")}),
            )
        return result

    def entity_handler(kind):
        async def handler(request: Request, user=Depends(authenticated)):
            params = request.query_params
            source_params = {
                k: v
                for k, v in params.items()
                if k.casefold() in {"includeitemtypes", "excludeitemtypes", "mediatypes"}
            }
            rows = entities(filter_items(all_items(request, user), source_params), kind)
            for row in rows:
                row["UserData"] = user_data(context(request), user, row["Id"])
            own_params = {
                k: v
                for k, v in params.items()
                if k.casefold() not in {"includeitemtypes", "excludeitemtypes", "mediatypes"}
            }
            return page(filter_items(rows, own_params), params)

        return handler

    for kind in ("Genre", "Studio", "Person", "Year"):
        app.add_api_route(
            "/" + {"Person": "Persons"}.get(kind, kind + "s"),
            entity_handler(kind),
            methods=["GET"],
            name="jellyfin_" + kind,
        )

    @app.get("/Search/Hints")
    async def hints(request: Request, user=Depends(authenticated)):
        params = request.query_params
        source = all_items(request, user)
        rows = list(source) if bool_parameter(params, "includeMedia", True) else []
        for kind, option in (
            ("Person", "includePeople"),
            ("Genre", "includeGenres"),
            ("Studio", "includeStudios"),
        ):
            if bool_parameter(params, option, True):
                rows.extend(entities(source, kind))
        result = page(filter_items(rows, params), params)
        return {
            "SearchHints": [
                {
                    **i,
                    "ItemId": i["Id"],
                    "MatchedTerm": parameter(params, "searchTerm", ""),
                    "PrimaryImageTag": i.get("ImageTags", {}).get("Primary"),
                    "Series": i.get("SeriesName"),
                }
                for i in result["Items"]
            ],
            "TotalRecordCount": result["TotalRecordCount"],
        }

    def by_name_handler(kind):
        async def handler(name: str, request: Request, user=Depends(authenticated)):
            check_user(request, user)
            return project(item_dto(context(request), named_id(kind, name), user), request.query_params)

        return handler

    for kind in ("Genre", "Studio", "Person", "Year"):
        app.add_api_route(
            "/" + {"Person": "Persons"}.get(kind, kind + "s") + "/{name}",
            by_name_handler(kind),
            methods=["GET"],
            name="jellyfin_" + kind + "_by_name",
        )

    def similar_items(source, baseline):
        genres = set(baseline.get("Genres", []))
        people = {p["Id"] for p in baseline.get("People", [])}

        def score(item):
            return 2 * len(genres.intersection(item.get("Genres", []))) + len(
                people.intersection(p["Id"] for p in item.get("People", []))
            )

        return sorted(
            (i for i in source if i["Id"] != baseline["Id"] and i["Type"] == baseline["Type"] and score(i)),
            key=lambda i: (-score(i), i["Name"]),
        )

    @app.get("/Items/{item_id}/Similar")
    @app.get("/Movies/{item_id}/Similar")
    @app.get("/Shows/{item_id}/Similar")
    async def similar(item_id: str, request: Request, user=Depends(authenticated)):
        return page(
            similar_items(all_items(request, user), item_dto(context(request), item_id, user)),
            request.query_params,
        )

    @app.get("/Items/Suggestions")
    async def suggestions(request: Request, user=Depends(authenticated)):
        params = dict(request.query_params)
        params.setdefault("includeItemTypes", parameter(params, "type", "Movie,Episode"))
        params.setdefault("mediaTypes", parameter(params, "mediaType", "Video"))
        params.setdefault("isPlayed", "false")
        params.setdefault("sortBy", "CommunityRating,DateCreated")
        params.setdefault("sortOrder", "Descending")
        return page(filter_items(all_items(request, user), params), params)

    @app.get("/Movies/Recommendations")
    async def recommendations(request: Request, user=Depends(authenticated)):
        items = all_items(request, user)
        baselines = [
            i
            for i in items
            if i["Type"] == "Movie"
            and (i.get("UserData", {}).get("IsFavorite") or i.get("UserData", {}).get("Played"))
        ]
        rows = []
        for baseline in baselines:
            candidates = similar_items(items, baseline)[
                : number_parameter(request.query_params, "itemLimit", 8)
            ]
            if candidates:
                rows.append(
                    {
                        "Items": [project(i, request.query_params) for i in candidates],
                        "RecommendationType": "SimilarToLikedItem"
                        if baseline["UserData"].get("IsFavorite")
                        else "SimilarToRecentlyPlayed",
                        "BaselineItemName": baseline["Name"],
                        "CategoryId": baseline["Id"],
                    }
                )
        return rows[: number_parameter(request.query_params, "categoryLimit", 5)]

    @app.get("/Items/{item_id}/Ancestors")
    async def ancestors(item_id: str, request: Request, user=Depends(authenticated)):
        check_user(request, user)
        item = item_dto(context(request), item_id, user)
        result = []
        seen = {item["Id"]}
        while item.get("ParentId") and item["ParentId"] not in seen:
            item = item_dto(context(request), item["ParentId"], user)
            result.append(project(item, request.query_params))
            seen.add(item["Id"])
        return result
