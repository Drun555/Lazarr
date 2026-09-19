import asyncio
from email.utils import formatdate

import httpx
import pytest

from lazarr.models import ConfigEntry, ProviderConfig
from lazarr.rate_limit import RequestPacer, retry_after
from lazarr.sdk import ProviderError
from lazarr.search import PREFIX, enqueue


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


async def test_concurrent_requests_are_spaced_and_providers_are_independent():
    clock = Clock()
    pacer = RequestPacer(clock=clock, sleep=clock.sleep)
    times = []

    async def request():
        await pacer.wait()
        times.append(clock())

    await asyncio.gather(*(request() for _ in range(4)))
    assert times == [1000, 1002, 1004, 1006]
    other = RequestPacer(clock=clock, sleep=clock.sleep)
    await other.wait()
    assert clock() == 1006


async def test_randomized_request_spacing_stays_inside_configured_range():
    clock = Clock()
    delays = iter([10, 30, 17, 22])
    pacer = RequestPacer(
        10,
        30,
        clock=clock,
        sleep=clock.sleep,
        choose_delay=lambda minimum, maximum: next(delays),
    )
    times = []
    for _ in range(4):
        await pacer.wait()
        times.append(clock())
    assert times == [1000, 1010, 1040, 1057]


def test_rutracker_uses_long_randomized_pacing_and_manual_urls_are_scoped(core):
    _, _, manager, _ = core
    manager.request_interval = 2
    pacer = manager.request_pacer("rutracker")
    assert (pacer.min_interval, pacer.max_interval) == (10, 30)
    manager.configure("rutracker", {}, True)
    item = manager.manual_candidate("https://rutracker.org/forum/viewtopic.php?t=12345")
    assert (item.provider, item.id) == ("rutracker", "12345")
    with pytest.raises(ValueError, match="не распознан"):
        manager.manual_candidate("https://attacker.example/viewtopic.php?t=12345")


async def test_cancelled_wait_does_not_reserve_a_request_slot():
    clock = Clock()
    waiting = asyncio.Event()

    async def sleep(seconds):
        waiting.set()
        await asyncio.Future()

    pacer = RequestPacer(clock=clock, sleep=sleep)
    await pacer.wait()
    task = asyncio.create_task(pacer.wait())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pacer.next_at == 1002
    pacer.sleep = clock.sleep
    await pacer.wait()
    assert clock() == 1002


async def test_requests_share_pacing_across_plugin_calls_and_direct_http(core):
    _, _, manager, _ = core
    clock, times = Clock(), []
    manager.request_pacers["nyaa"] = RequestPacer(clock=clock, sleep=clock.sleep)
    manager.configure("nyaa", {}, True)

    def respond(request):
        times.append(clock())
        return httpx.Response(200, text="ok")

    manager.transport = httpx.MockTransport(respond)
    async with manager.open("nyaa") as provider:
        await provider.ctx.request("GET", "https://nyaa.si/")
        await provider.ctx.http.get("https://nyaa.si/direct")
    async with manager.open("nyaa") as provider:
        await provider.ctx.request("GET", "https://nyaa.si/")
    assert times == [1000, 1002, 1004]


@pytest.mark.parametrize(
    "value,expected", [(None, 60), ("90", 90), ("0", 1), ("bad", 60), (formatdate(1120, usegmt=True), 120)]
)
def test_retry_after_seconds_and_http_date(value, expected):
    assert retry_after(value, now=1000) == expected


async def test_backoff_persists_grows_honors_header_and_blocks_manual_checks(core, monkeypatch):
    _, db, manager, _ = core
    clock, calls = Clock(), []
    monkeypatch.setattr("lazarr.plugins.time.time", clock)
    manager.configure("nyaa", {}, True)
    response = httpx.Response(504)

    def respond(request):
        calls.append(request.url)
        return response

    manager.transport = httpx.MockTransport(respond)
    for expected in [60, 120, 240]:
        with pytest.raises(ProviderError, match="504"):
            async with manager.open("nyaa") as provider:
                await provider.ctx.request("GET", "https://nyaa.si/")
        with db.session() as session:
            assert session.get(ProviderConfig, "nyaa").retry_at == clock() + expected
        before = len(calls)
        with pytest.raises(ProviderError, match="504"):
            async with manager.open("nyaa", allow_disabled=True):
                pytest.fail("Manual check bypassed backoff")
        assert len(calls) == before
        clock.now += expected
    response = httpx.Response(429, headers={"Retry-After": formatdate(clock() + 900, usegmt=True)})
    with pytest.raises(ProviderError) as error:
        async with manager.open("nyaa") as provider:
            await provider.ctx.request("GET", "https://nyaa.si/")
    assert error.value.retry_after == 900
    with db.session() as session:
        assert session.get(ProviderConfig, "nyaa").retry_at == clock() + 900
    clock.now += 900
    response = httpx.Response(200)
    async with manager.open("nyaa") as provider:
        await provider.ctx.request("GET", "https://nyaa.si/")
    with db.session() as session:
        assert session.get(ConfigEntry, "provider.backoff.nyaa") is None
        assert session.get(ProviderConfig, "nyaa").last_error is None


def test_manual_debounce_survives_completed_search_and_does_not_delay_new_tasks(core, monkeypatch):
    _, db, _, _ = core
    clock = Clock()
    monkeypatch.setattr("lazarr.search.time.time", clock)
    with db.session() as session:
        enqueue(session)
    with db.session() as session:
        session.delete(session.get(ConfigEntry, PREFIX + "all"))
    clock.now += 1
    with db.session() as session:
        enqueue(session)
        enqueue(session, 42)
    with db.session() as session:
        assert session.get(ConfigEntry, PREFIX + "all") is None
        assert session.get(ConfigEntry, PREFIX + "42") is not None
    clock.now += 1
    with db.session() as session:
        enqueue(session)
    with db.session() as session:
        assert session.get(ConfigEntry, PREFIX + "all") is not None
