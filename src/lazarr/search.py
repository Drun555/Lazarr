"""Durable search requests and a bounded, credential-free activity snapshot."""

import time
from copy import deepcopy
from lazarr.models import ConfigEntry

PREFIX = "search.request."


def enqueue(db, task_id=None):
    key = PREFIX + (str(task_id) if task_id is not None else "all")
    if task_id is None:
        # Leading-edge debounce: accept the first click immediately and coalesce
        # repeats even when a very short search already completed.
        last = db.get(ConfigEntry, "search.manual")
        now = time.time()
        if last and now - last.value["accepted_at"] < 2:
            return key
        if last:
            last.value = {"accepted_at": now}
        else:
            db.add(ConfigEntry(key="search.manual", value={"accepted_at": now}))
    if not db.get(ConfigEntry, key):
        db.add(ConfigEntry(key=key, value={"task_id": task_id, "created_at": time.time()}))
    return key


class SearchProgress:
    def __init__(self):
        self.value = {
            "running": False,
            "state": "idle",
            "stage": "idle",
            "message": "Поиск ещё не запускался",
            "groups_total": 0,
            "groups_done": 0,
            "candidates_found": 0,
            "candidates_checked": 0,
            "candidates_filtered": 0,
            "candidates_deferred": 0,
            "candidates_failed": 0,
            "history": [],
            "errors": 0,
            "providers": [],
            "search_requests": 0,
        }

    def begin(self):
        self.__init__()
        self.value.update(running=True, state="running", started_at=time.time())
        self.record("prepare", "Проверка очереди и дат выхода")

    def record(self, stage, message, **values):
        self.value.update(values, stage=stage, message=message, updated_at=time.time())
        self.value["history"] = [
            *self.value["history"][-39:],
            {
                "time": time.time(),
                "stage": stage,
                "message": message,
                "provider": self.value.get("provider", ""),
            },
        ]

    def snapshot(self):
        return deepcopy(self.value)


def defer(db, task_ids, provider, until):
    """Persist a retry of tasks, not a cache of candidates or provider responses."""
    for task_id in set(task_ids):
        key = f"{PREFIX}retry.{provider}.{task_id}"
        value = {"task_id": task_id, "provider": provider, "not_before": until, "created_at": time.time()}
        row = db.get(ConfigEntry, key)
        if row:
            row.value = value
        else:
            db.add(ConfigEntry(key=key, value=value))
