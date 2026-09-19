"""Shared spacing of outgoing provider requests and Retry-After parsing."""

import asyncio
import math
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


class RequestPacer:
    def __init__(
        self,
        interval=2.0,
        max_interval=None,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        choose_delay=random.uniform,
    ):
        self.interval = interval
        self.min_interval = interval
        self.max_interval = interval if max_interval is None else max_interval
        if self.min_interval < 0 or self.max_interval < self.min_interval:
            raise ValueError("Invalid request pacing interval")
        self.clock, self.sleep = clock, sleep
        self.choose_delay = choose_delay
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    def delay(self):
        if self.min_interval == self.max_interval:
            return self.min_interval
        return self.choose_delay(self.min_interval, self.max_interval)

    async def wait(self, request=None):
        async with self.lock:
            delay = max(0, self.next_at - self.clock())
            if delay:
                await self.sleep(delay)
            self.next_at = self.clock() + self.delay()


def retry_after(value, default=60, now=None):
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return max(1, int(value))
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(1, math.ceil(stamp.timestamp() - (time.time() if now is None else now)))
    except (ValueError, TypeError, OverflowError):
        return default
