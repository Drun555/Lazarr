"""Required login fields from the Jellyfin Kotlin SDK used by Wholphin."""

import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from lazarr.app import create_app
from test_jellyfin import jellyfin_login


CONTRACT = json.loads((Path(__file__).parent / "fixtures/jellyfin-login-required-fields.json").read_text())
MODELS = CONTRACT["models"]


def assert_model(value, model):
    for field, kind in MODELS[model].items():
        assert field in value, f"{model}.{field} is required by the SDK"
        item = value[field]
        if kind in {"Boolean", "Int", "Long", "String"}:
            assert type(item) is {"Boolean": bool, "Int": int, "Long": int, "String": str}[kind], field
        elif kind == "UUID":
            uuid.UUID(item)
        elif kind == "DateTime":
            assert datetime.fromisoformat(item).tzinfo is not None
        elif kind.startswith("List<"):
            assert isinstance(item, list), field
            for entry in item:
                if kind == "List<UUID>":
                    uuid.UUID(entry)
                elif kind[5:-1] in CONTRACT["enums"]:
                    assert entry in CONTRACT["enums"][kind[5:-1]]
                elif kind[5:-1] in MODELS:
                    assert_model(entry, kind[5:-1])
                else:
                    raise AssertionError(f"Unchecked SDK list type: {kind}")
        elif kind in CONTRACT["enums"]:
            assert item in CONTRACT["enums"][kind]
        else:
            raise AssertionError(f"Unchecked SDK type: {kind}")


def assert_user(user):
    assert_model(user, "UserDto")
    assert_model(user["Configuration"], "UserConfiguration")
    assert_model(user["Policy"], "UserPolicy")


def test_login_and_user_reads_satisfy_kotlin_sdk(core):
    with TestClient(create_app(core[0])) as client:
        auth = jellyfin_login(client)
        assert_user(auth["User"])
        assert_model(auth["SessionInfo"], "SessionInfoDto")
        assert auth["SessionInfo"]["UserId"] == auth["User"]["Id"]
        # A partial preferences update must retain defaults required on later logins.
        changed = {"HidePlayedInLatest": False, "OrderedViews": [str(uuid.uuid4())]}
        assert client.post("/Users/Configuration", json=changed).status_code == 204
        for path in ("/Users/Me", f"/Users/{auth['User']['Id']}"):
            response = client.get(path)
            assert response.status_code == 200
            assert_user(response.json())
            assert response.json()["Configuration"].items() >= changed.items()
        assert_user(jellyfin_login(client)["User"])
