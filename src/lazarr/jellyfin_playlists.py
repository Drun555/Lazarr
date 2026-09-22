"""Video-only Jellyfin playlists with owner-managed sharing."""

import uuid

from fastapi import Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import delete, select, text

from lazarr.jellyfin_state import csv_parameter, number_parameter, page, parameter
from lazarr.models import PlaybackProgress, User, VideoPlaylist


class Share(BaseModel):
    UserId: uuid.UUID
    CanEdit: StrictBool = False


class PlaylistUpdate(BaseModel):
    Name: str | None = Field(default=None, max_length=256)
    Ids: list[uuid.UUID] | None = None
    Users: list[Share] | None = None
    IsPublic: StrictBool | None = None
    UserId: uuid.UUID | None = None
    MediaType: str | None = None


class ShareUpdate(BaseModel):
    CanEdit: StrictBool | None = None


def access(db, identity, user, permission="read"):
    from lazarr.jellyfin import parse_object_id

    kind, key, _ = parse_object_id(identity)
    row = db.get(VideoPlaylist, key) if kind == "playlist" else None
    if row is None:
        raise HTTPException(404, "Playlist not found")
    owner = row.owner_id == user.id
    shared = str(user.id) in row.shares
    if not owner and not row.is_public and not shared:
        raise HTTPException(404, "Playlist not found")
    if permission == "owner" and not owner:
        raise HTTPException(403, "Only the playlist owner can manage sharing or delete the playlist")
    if permission == "edit" and not owner and not row.shares.get(str(user.id), False):
        raise HTTPException(403, "Playlist is read-only")
    return row


def shares_dto(ctx, row):
    from lazarr.jellyfin import user_object_id

    return [{"UserId": user_object_id(ctx, int(key)), "CanEdit": edit} for key, edit in row.shares.items()]


def dto(ctx, row, user):
    from lazarr.jellyfin import object_id, server_id, user_data

    identity = object_id("playlist", row.id)
    return {
        "Id": identity,
        "Name": row.name,
        "ServerId": server_id(ctx),
        "Type": "Playlist",
        "IsFolder": True,
        "MediaType": "Video",
        "ChildCount": len(row.entries),
        "CanDelete": row.owner_id == user.id,
        "CanDownload": False,
        "ImageTags": {},
        "BackdropImageTags": [],
        "UserData": user_data(ctx, user, identity),
    }


def visible(ctx, user):
    with ctx.db.session() as db:
        return [
            dto(ctx, row, user)
            for row in db.scalars(select(VideoPlaylist).order_by(VideoPlaylist.name, VideoPlaylist.id))
            if row.owner_id == user.id or row.is_public or str(user.id) in row.shares
        ]


def playlist_item(ctx, identity, user):
    if user is None:
        raise HTTPException(401, "Authentication required")
    with ctx.db.session() as db:
        return dto(ctx, access(db, identity, user), user)


def contents(ctx, identity, user):
    from lazarr.jellyfin import item_dto

    with ctx.db.session() as db:
        row = access(db, identity, user)
        entries = list(row.entries)
    result = []
    for entry in entries:
        try:
            item = item_dto(ctx, entry["item_id"], user)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue  # Removed library items do not break the remaining playlist.
        result.append({**item, "PlaylistItemId": entry["id"]})
    return result


def validated_entries(ctx, user, ids):
    from lazarr.jellyfin import item_dto

    entries = []
    for value in ids:
        identity = str(value)
        item = item_dto(ctx, identity, user)
        if item.get("MediaType") != "Video" or item.get("IsFolder"):
            raise HTTPException(400, "Only individual videos can be added to a video playlist")
        entries.append({"id": uuid.uuid4().hex, "item_id": item["Id"]})
    return entries


def share_user(ctx, db, identity):
    from lazarr.jellyfin import parse_object_id, user_object_id

    kind, key, _ = parse_object_id(str(identity))
    user = db.get(User, key) if kind == "user" else None
    if not user or not user.active or uuid.UUID(str(identity)) != uuid.UUID(user_object_id(ctx, key)):
        raise HTTPException(404, "User not found")
    return user


def validate_shares(ctx, db, owner_id, shares):
    result = {}
    for share in shares:
        target = share_user(ctx, db, share.UserId)
        if target.id != owner_id:
            result[str(target.id)] = share.CanEdit
    return result


def install_playlist_api(app, context, authenticated, require_user_id):
    def check_user(request, user):
        identity = parameter(request.query_params, "userId")
        if identity:
            require_user_id(identity, user)

    @app.post("/Playlists")
    async def create(
        request: Request, payload: PlaylistUpdate | None = Body(default=None), user=Depends(authenticated)
    ):
        from lazarr.jellyfin import object_id

        check_user(request, user)
        payload = payload or PlaylistUpdate()
        if payload.UserId:
            require_user_id(str(payload.UserId), user)
        name = parameter(request.query_params, "name", payload.Name)
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise HTTPException(400, "Playlist name is required (maximum 256 characters)")
        if parameter(request.query_params, "mediaType", payload.MediaType or "Video") not in {
            "Video",
            "Unknown",
        }:
            raise HTTPException(400, "Only video playlists are supported")
        ids = csv_parameter(request.query_params, "ids") or payload.Ids or []
        ctx = context(request)
        entries = validated_entries(ctx, user, ids)
        with ctx.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            shares = validate_shares(ctx, db, user.id, payload.Users or [])
            row = VideoPlaylist(
                owner_id=user.id,
                name=name.strip(),
                entries=entries,
                shares=shares,
                is_public=payload.IsPublic or False,
            )
            db.add(row)
            db.flush()
            identity = object_id("playlist", row.id)
        return {"Id": identity}

    @app.get("/Playlists/{playlist_id}")
    async def get(playlist_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            row = access(db, playlist_id, user)
            return {
                "OpenAccess": row.is_public,
                "Shares": shares_dto(ctx, row),
                "ItemIds": [e["item_id"] for e in row.entries],
            }

    @app.post("/Playlists/{playlist_id}", status_code=204)
    async def update(
        playlist_id: str, payload: PlaylistUpdate, request: Request, user=Depends(authenticated)
    ):
        ctx = context(request)
        # Check permission before looking up any supplied items.
        with ctx.db.session() as db:
            access(db, playlist_id, user, "edit")
        entries = validated_entries(ctx, user, payload.Ids) if payload.Ids is not None else None
        with ctx.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "edit")
            if payload.Users is not None or payload.IsPublic is not None:
                access(db, playlist_id, user, "owner")
            if payload.Name is not None:
                if not payload.Name.strip():
                    raise HTTPException(400, "Playlist name cannot be empty")
                row.name = payload.Name.strip()
            if entries is not None:
                row.entries = entries
            if payload.Users is not None:
                row.shares = validate_shares(ctx, db, row.owner_id, payload.Users)
            if payload.IsPublic is not None:
                row.is_public = payload.IsPublic
        return Response(status_code=204)

    @app.get("/Playlists/{playlist_id}/Items")
    async def items(playlist_id: str, request: Request, user=Depends(authenticated)):
        check_user(request, user)
        return page(contents(context(request), playlist_id, user), request.query_params)

    @app.post("/Playlists/{playlist_id}/Items", status_code=204)
    async def add(playlist_id: str, request: Request, user=Depends(authenticated)):
        check_user(request, user)
        ctx = context(request)
        with ctx.db.session() as db:
            access(db, playlist_id, user, "edit")
        entries = validated_entries(ctx, user, csv_parameter(request.query_params, "ids"))
        with ctx.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "edit")
            position = number_parameter(request.query_params, "position", len(row.entries))
            if position > len(row.entries):
                raise HTTPException(400, "Position is outside playlist")
            row.entries = row.entries[:position] + entries + row.entries[position:]
        return Response(status_code=204)

    @app.delete("/Playlists/{playlist_id}/Items", status_code=204)
    async def remove(playlist_id: str, request: Request, user=Depends(authenticated)):
        ids = set(csv_parameter(request.query_params, "entryIds"))
        with context(request).db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "edit")
            row.entries = [e for e in row.entries if e["id"] not in ids]
        return Response(status_code=204)

    @app.post("/Playlists/{playlist_id}/Items/{entry_id}/Move/{new_index}", status_code=204)
    async def move(
        playlist_id: str, entry_id: str, new_index: int, request: Request, user=Depends(authenticated)
    ):
        with context(request).db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "edit")
            entries = list(row.entries)
            index = next((n for n, e in enumerate(entries) if e["id"] == entry_id), None)
            if index is None:
                raise HTTPException(404, "Playlist entry not found")
            if not 0 <= new_index < len(entries):
                raise HTTPException(400, "Position is outside playlist")
            entries.insert(new_index, entries.pop(index))
            row.entries = entries
        return Response(status_code=204)

    @app.get("/Playlists/{playlist_id}/Users")
    async def users(playlist_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            return shares_dto(ctx, access(db, playlist_id, user, "owner"))

    @app.get("/Playlists/{playlist_id}/Users/{user_id}")
    async def get_share(playlist_id: str, user_id: str, request: Request, user=Depends(authenticated)):
        from lazarr.jellyfin import user_object_id

        ctx = context(request)
        with ctx.db.session() as db:
            row = access(db, playlist_id, user)
            target = share_user(ctx, db, user_id)
            if user.id not in {row.owner_id, target.id}:
                raise HTTPException(403, "Cannot read another user's permissions")
            if target.id != row.owner_id and str(target.id) not in row.shares:
                raise HTTPException(404, "Playlist user not found")
            return {
                "UserId": user_object_id(ctx, target.id),
                "CanEdit": target.id == row.owner_id or row.shares.get(str(target.id), False),
            }

    @app.post("/Playlists/{playlist_id}/Users/{user_id}", status_code=204)
    async def set_share(
        playlist_id: str, user_id: str, payload: ShareUpdate, request: Request, user=Depends(authenticated)
    ):
        ctx = context(request)
        with ctx.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "owner")
            target = share_user(ctx, db, user_id)
            if target.id == row.owner_id:
                raise HTTPException(400, "Owner permissions cannot be changed")
            row.shares = {
                **row.shares,
                str(target.id): payload.CanEdit
                if payload.CanEdit is not None
                else row.shares.get(str(target.id), False),
            }
        return Response(status_code=204)

    @app.delete("/Playlists/{playlist_id}/Users/{user_id}", status_code=204)
    async def delete_share(playlist_id: str, user_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, playlist_id, user, "owner")
            target = share_user(ctx, db, user_id)
            if target.id == row.owner_id:
                raise HTTPException(400, "Owner cannot be removed")
            row.shares = {k: v for k, v in row.shares.items() if k != str(target.id)}
        return Response(status_code=204)

    @app.delete("/Items/{item_id}", status_code=204)
    async def delete_playlist(item_id: str, request: Request, user=Depends(authenticated)):
        from lazarr.jellyfin import parse_object_id

        if parse_object_id(item_id)[0] != "playlist":
            raise HTTPException(405, "Library editing is disabled")
        with context(request).db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = access(db, item_id, user, "owner")
            db.delete(row)
            db.execute(delete(PlaybackProgress).where(PlaybackProgress.item_id == str(uuid.UUID(item_id))))
        return Response(status_code=204)
