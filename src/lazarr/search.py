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
    row = db.get(ConfigEntry, key)
    if row is not None and task_id is not None:
        row.value = {"task_id": task_id, "created_at": time.time()}
    elif row is None:
        db.add(ConfigEntry(key=key, value={"task_id": task_id, "created_at": time.time()}))
    return key


class SearchProgress:
    def __init__(self):
        self.tasks = {}
        self.active_tasks = []
        self.baseline = {}
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
        tasks = self.tasks
        self.__init__()
        self.tasks = tasks
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
        self.sync_tasks()
        for identity in self.active_tasks:
            task = self.tasks[identity]
            task["history"] = [*task["history"][-39:], deepcopy(self.value["history"][-1])]

    def prepare_tasks(self, groups):
        seen = set()
        for ids in groups:
            for identity in ids:
                if identity not in seen:
                    self.tasks[identity] = SearchProgress().snapshot()
                    self.tasks[identity].update(
                        state="queued", message="Ожидает поиска", started_at=time.time()
                    )
                    seen.add(identity)
                self.tasks[identity]["groups_total"] += 1

    def start_group(self, identities):
        self.active_tasks = identities
        self.baseline = {key: self.value[key] for key in self.counters}
        for identity in identities:
            self.tasks[identity].update(running=True, state="running")

    counters = (
        "candidates_found",
        "candidates_checked",
        "candidates_filtered",
        "candidates_deferred",
        "candidates_failed",
        "errors",
        "search_requests",
    )

    def sync_tasks(self):
        for identity in self.active_tasks:
            task = self.tasks[identity]
            for key in self.counters:
                task[key] += self.value[key] - self.baseline[key]
            for key in ("message", "stage", "provider", "season", "episodes", "candidate", "updated_at"):
                if key in self.value:
                    task[key] = deepcopy(self.value[key])
        self.baseline = {key: self.value[key] for key in self.counters}

    def finish_group(self, completed=True):
        self.sync_tasks()
        for identity in self.active_tasks:
            task = self.tasks[identity]
            if not completed:
                task.update(
                    running=False, state="queued", message="Поиск прерван; ожидает повторного запуска"
                )
                continue
            task["groups_done"] += 1
            done = task["groups_done"] == task["groups_total"]
            task.update(
                running=False, state=("error" if task["errors"] else "finished") if done else "queued"
            )
        self.active_tasks = []

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
