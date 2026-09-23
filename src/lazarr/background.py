"""Bounded, observable worker lanes for request-triggered background work."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import contextmanager
from collections import deque
from functools import partial
import threading
import time
import uuid

from fastapi import HTTPException


# Request-scoped response assembly is not user-facing background processing.
# Keep it in the bounded executor, but out of the Processes list and history.
API_REQUEST_KINDS = frozenset({"catalog", "latest", "item-detail", "library-detail", "next-up"})


class BackgroundTasks:
    def __init__(self, capacity=64):
        # Long ffmpeg jobs must not starve interactive catalogue calculations.
        self.pools = {
            "catalog": ThreadPoolExecutor(max_workers=1, thread_name_prefix="lazarr-catalog"),
            "media": ThreadPoolExecutor(max_workers=1, thread_name_prefix="lazarr-media"),
        }
        self.capacity = capacity
        self.lock = threading.RLock()
        self.active = {}
        self.shared = {}
        self.history = deque(maxlen=30)
        self.closed = False

    async def run(self, kind, function, *args, lane="media", key=None, owner_id=None):
        """A disconnected waiter does not cancel work shared with other clients."""
        identity = uuid.uuid4().hex
        shared_key = (lane, kind, owner_id, key) if key is not None else None
        with self.lock:
            if self.closed:
                raise HTTPException(503, "Background workers are stopping", headers={"Retry-After": "5"})
            future = self.shared.get(shared_key) if shared_key else None
            if future is None:
                if len(self.active) >= self.capacity:
                    raise HTTPException(503, "Background queue is full", headers={"Retry-After": "5"})
                job = {
                    "id": identity,
                    "kind": kind,
                    "lane": lane,
                    "state": "queued",
                    "created_at": time.time(),
                    "started_at": None,
                    "finished_at": None,
                    "owner_id": owner_id,
                }
                self.active[identity] = job
                context = copy_context()
                future = self.pools[lane].submit(self._execute, job, context, partial(function, *args))
                if shared_key:
                    self.shared[shared_key] = future
                future.add_done_callback(lambda done: self._finished(job, shared_key, done))
        return await asyncio.shield(asyncio.wrap_future(future))

    def _execute(self, job, context, function):
        with self.lock:
            job.update(state="running", started_at=time.time())
        return context.run(function)

    def _finished(self, job, key, future):
        with self.lock:
            job.update(state="failed" if future.exception() else "completed", finished_at=time.time())
            # Never expose exceptions containing paths, URLs or credentials in the UI.
            self.active.pop(job["id"], None)
            if key:
                self.shared.pop(key, None)
            if job["kind"] not in API_REQUEST_KINDS:
                self.history.append(dict(job))

    def snapshot(self, user_id):
        with self.lock:
            rows = [
                dict(row)
                for row in [*self.active.values(), *reversed(self.history)]
                if row["owner_id"] in {None, user_id} and row["kind"] not in API_REQUEST_KINDS
            ]
        for row in rows:
            row.pop("owner_id")
        return {"items": rows, "capacity": self.capacity}

    @contextmanager
    def observe(self, kind, *, detail=None):
        """Track an async, IO-bound maintenance phase without moving its event loop."""
        with self.lock:
            if self.closed or len(self.active) >= self.capacity:
                raise HTTPException(503, "Background queue unavailable")
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "lane": "metadata",
                "state": "running",
                "created_at": time.time(),
                "started_at": time.time(),
                "finished_at": None,
                "owner_id": None,
                "detail": detail,
            }
            self.active[job["id"]] = job
        state = "failed"
        try:
            yield
            state = "completed"
        finally:
            with self.lock:
                job.update(state=state, finished_at=time.time())
                self.active.pop(job["id"], None)
                self.history.append(dict(job))

    async def close(self):
        with self.lock:
            self.closed = True
        # Drain accepted work before disposing the DB / releasing the instance lock.
        for pool in self.pools.values():
            await asyncio.to_thread(pool.shutdown, wait=True)
