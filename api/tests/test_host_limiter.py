"""E.8 + E.9 — domain helper, per-host limiter, its activities, the workflow helper."""

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from app.core import host_limiter
from app.core.domains import registrable_domain
from app.main import app
from app.workflows import steps
from app.workflows.activities import host_slots
from app.workflows.activities.contracts import (
    SCRAPE_ACTIVITY,
    ScrapeInput,
    ScrapeOutput,
    StoredObject,
)
from app.workflows.queues import SCRAPE_HTTP_QUEUE, WORKFLOW_QUEUE
from app.workflows.steps import (
    ACQUIRE_HOST_SLOT_ACTIVITY,
    HOST_WAIT_CAP,
    RELEASE_HOST_SLOT_ACTIVITY,
    HostSlot,
    HostSlotInput,
    HostSlotRelease,
)

# ── registrable_domain ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.shop.com/a", "shop.com"),
        ("https://img.cdn.shop.com/a.png", "shop.com"),
        ("https://a.b.example.co.uk/", "example.co.uk"),
        ("https://alice.github.io/", "alice.github.io"),
        ("https://bob.github.io/", "bob.github.io"),
        ("https://x.blogspot.com/", "x.blogspot.com"),
        # Hosts that are themselves a public suffix keep the exact host.
        ("https://github.io/", "github.io"),
        ("https://s3.amazonaws.com/bucket", "s3.amazonaws.com"),
        # IPs and single-label / unknown-TLD hosts keep the exact host.
        ("http://1.2.3.4:8080/", "1.2.3.4"),
        ("http://[0:0::1]/", "::1"),
        ("http://localhost:8000/", "localhost"),
        ("http://localhost./", "localhost"),
        ("http://foo.internal/", "foo.internal"),
        # Normalised: case, port, userinfo, trailing dot, IDN form.
        ("https://User:pw@WWW.Shop.COM.:8443/x", "shop.com"),
        ("https://www.münchen.de/", "xn--mnchen-3ya.de"),
        ("https://xn--mnchen-3ya.de/", "xn--mnchen-3ya.de"),
    ],
)
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected


@pytest.mark.parametrize("url", ["", "not a url", "file:///etc/passwd"])
def test_registrable_domain_no_host(url):
    with pytest.raises(ValueError):
        registrable_domain(url)


def test_suffix_list_is_offline():
    from app.core import domains

    assert domains._extract.suffix_list_urls == ()
    assert domains._extract.include_psl_private_domains is True


# ── host_limiter (real Redis) ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def redis_client(init_clients):
    async with aioredis.Redis(connection_pool=app.state.redis_pool) as r:
        yield r


@pytest_asyncio.fixture
async def domain(redis_client):
    d = f"t-{uuid.uuid4().hex}.test"
    yield d
    await redis_client.delete(*host_limiter._keys(d))


async def _acquire(r, domain, lease_id, ttl_ms=60_000, **limits):
    return await host_limiter.acquire(r, domain, lease_id, ttl_ms, **limits)


async def test_rate_window_delays_second_start(redis_client, domain):
    assert await _acquire(redis_client, domain, "a") == 0
    wait = await _acquire(redis_client, domain, "b")
    assert 0 < wait <= 1000 + host_limiter.JITTER_MS


async def test_rate_window_reopens(redis_client, domain):
    assert await _acquire(redis_client, domain, "a", interval_ms=50) == 0
    await asyncio.sleep(0.1)
    assert await _acquire(redis_client, domain, "b", interval_ms=50) == 0


async def test_in_flight_cap(redis_client, domain):
    assert await _acquire(redis_client, domain, "a", interval_ms=0) == 0
    assert await _acquire(redis_client, domain, "b", interval_ms=0) == 0
    wait = await _acquire(redis_client, domain, "c", interval_ms=0)
    assert host_limiter.BUSY_POLL_MS <= wait <= host_limiter.BUSY_POLL_MS + host_limiter.JITTER_MS

    await host_limiter.release(redis_client, domain, "a")
    assert await _acquire(redis_client, domain, "c", interval_ms=0) == 0


async def test_retried_acquire_holds_one_lease(redis_client, domain):
    assert await _acquire(redis_client, domain, "a") == 0
    # Inside the rate window and at the cap: a retry of a held lease still admits.
    assert await _acquire(redis_client, domain, "a", max_in_flight=1) == 0
    leases, _ = host_limiter._keys(domain)
    assert await redis_client.zcard(leases) == 1


async def test_expired_lease_frees_its_slot(redis_client, domain):
    # A live lease beside it keeps the key alive: the expired one must be trimmed, not timed out.
    assert await _acquire(redis_client, domain, "long", interval_ms=0) == 0
    assert await _acquire(redis_client, domain, "a", ttl_ms=50, interval_ms=0) == 0
    assert await _acquire(redis_client, domain, "b", interval_ms=0) > 0
    await asyncio.sleep(0.1)
    assert await _acquire(redis_client, domain, "b", interval_ms=0) == 0


async def test_keys_expire(redis_client, domain):
    assert await _acquire(redis_client, domain, "a", ttl_ms=5_000, interval_ms=0) == 0
    assert await _acquire(redis_client, domain, "b", ttl_ms=30_000) == 0
    leases, last = host_limiter._keys(domain)
    # The leases key lives as long as its longest lease.
    assert 25_000 < await redis_client.pttl(leases) <= 30_000
    assert 0 < await redis_client.pttl(last) <= 1000


async def test_release_unknown_lease_is_noop(redis_client, domain):
    await host_limiter.release(redis_client, domain, "never-acquired")
    assert await _acquire(redis_client, domain, "a") == 0


# ── activities ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def worker_redis(monkeypatch):
    # A fresh client per test: the module-level one would outlive the test's event loop.
    client = aioredis.Redis.from_url(host_slots.settings.redis_url, decode_responses=True)
    monkeypatch.setattr(host_slots, "_redis", client)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def dead_redis(monkeypatch):
    client = aioredis.Redis.from_url(
        "redis://redis:1/0", socket_connect_timeout=0.5, socket_timeout=0.5
    )
    monkeypatch.setattr(host_slots, "_redis", client)
    yield client
    await client.aclose()


def _slot_input(url, lease_id="lease-1"):
    return HostSlotInput(url=url, lease_id=lease_id, lease_ttl_ms=60_000)


async def test_acquire_activity(worker_redis):
    d = f"t-{uuid.uuid4().hex}.com"
    try:
        env = ActivityEnvironment()
        slot = await env.run(host_slots.acquire_host_slot, _slot_input(f"https://www.{d}/x"))
        assert slot == HostSlot(domain=d, wait_ms=0)
        busy = await env.run(host_slots.acquire_host_slot, _slot_input(f"https://{d}/", "lease-2"))
        assert busy.domain == d and busy.wait_ms > 0

        await env.run(host_slots.release_host_slot, HostSlotRelease(domain=d, lease_id="lease-1"))
        leases, _ = host_limiter._keys(d)
        assert await worker_redis.zcard(leases) == 0
    finally:
        await worker_redis.delete(*host_limiter._keys(d))


async def test_acquire_activity_fails_open_on_redis_error(dead_redis):
    slot = await ActivityEnvironment().run(
        host_slots.acquire_host_slot, _slot_input("https://shop.com/")
    )
    assert slot == HostSlot()


async def test_release_activity_fails_open_on_redis_error(dead_redis):
    await ActivityEnvironment().run(
        host_slots.release_host_slot, HostSlotRelease(domain="shop.com", lease_id="x")
    )


async def test_acquire_activity_without_host_goes():
    slot = await ActivityEnvironment().run(host_slots.acquire_host_slot, _slot_input("nonsense"))
    assert slot == HostSlot()


# ── lease TTL ────────────────────────────────────────────────────────────────


def test_host_lease_ttl():
    http = ScrapeInput(artifact_id="r1", url="https://a.com", output_format="html")
    pw = http.model_copy(update={"playwright_options": {"timeout_seconds": 60}})
    # 3 attempts x (5 min queue timer + run) + 1 s + 2 s backoff + 60 s margin
    assert steps.host_lease_ttl(http, "http") == timedelta(seconds=3 * (300 + 90) + 3 + 60)
    assert steps.host_lease_ttl(pw, "playwright") == timedelta(seconds=3 * (300 + 180) + 3 + 60)


def test_only_scrapes_get_the_long_queue_timer():
    assert steps.SCRAPE_SCHEDULE_TO_START == timedelta(minutes=5)
    assert steps.SCHEDULE_TO_START == timedelta(seconds=60)


# ── workflow helper ──────────────────────────────────────────────────────────

SCRAPE_INPUT = ScrapeInput(artifact_id="r1", url="https://www.shop.com/p", output_format="html")
SCRAPE_OUTPUT = ScrapeOutput(
    result=StoredObject(path="b/history/r1/scrape.html", size=1), content_hash="00c0ffee12345678"
)
BUSY = HostSlot(domain="shop.com", wait_ms=1000)


@workflow.defn(sandboxed=False)
class HostSlotHarness:
    @workflow.run
    async def run(self, input: ScrapeInput) -> ScrapeOutput:
        held = await steps.wait_for_host_slot(input.url, steps.host_lease_ttl(input, "http"))
        try:
            return await steps.scrape(input, "http")
        finally:
            await steps.release_host_slot(held)


class Harness:
    def __init__(self, slots=(), scrape=None, acquire_error=False, acquire_gate=None):
        self.slots = list(slots)
        self.acquired: list[HostSlotInput] = []
        self.released: list[HostSlotRelease] = []
        self.scraped = 0
        self.asked = asyncio.Event()
        self.asked_at: list = []
        self._scrape = scrape
        self._acquire_error = acquire_error
        # Awaited inside the acquire activity, after it is called.
        self._acquire_gate = acquire_gate

    def activities(self):
        @activity.defn(name=ACQUIRE_HOST_SLOT_ACTIVITY)
        async def acquire(input: HostSlotInput) -> HostSlot:
            self.acquired.append(input)
            self.asked_at.append(activity.info().current_attempt_scheduled_time)
            self.asked.set()
            if self._acquire_gate:
                await self._acquire_gate.wait()
            if self._acquire_error:
                raise RuntimeError("worker broke")
            return self.slots.pop(0) if self.slots else HostSlot(domain="shop.com")

        @activity.defn(name=RELEASE_HOST_SLOT_ACTIVITY)
        async def release(input: HostSlotRelease) -> None:
            self.released.append(input)

        @activity.defn(name=SCRAPE_ACTIVITY)
        async def scrape(input: ScrapeInput) -> ScrapeOutput:
            self.scraped += 1
            if self._scrape:
                return await self._scrape()
            return SCRAPE_OUTPUT

        return [acquire, release], scrape

    async def run(self, during=None):
        slot_activities, scrape = self.activities()
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(
                    env.client,
                    task_queue=WORKFLOW_QUEUE,
                    workflows=[HostSlotHarness],
                    activities=slot_activities,
                ),
                Worker(env.client, task_queue=SCRAPE_HTTP_QUEUE, activities=[scrape]),
            ):
                handle = await env.client.start_workflow(
                    HostSlotHarness.run,
                    SCRAPE_INPUT,
                    id=f"host-slot-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                )
                if during:
                    await during(handle)
                try:
                    return await handle.result()
                except WorkflowFailureError as exc:
                    return exc


async def test_slot_held_around_the_scrape():
    h = Harness()
    assert await h.run() == SCRAPE_OUTPUT
    assert h.scraped == 1
    (acquired,) = h.acquired
    assert acquired.url == SCRAPE_INPUT.url
    assert (
        acquired.lease_ttl_ms == steps.host_lease_ttl(SCRAPE_INPUT, "http").total_seconds() * 1000
    )
    assert h.released == [HostSlotRelease(domain="shop.com", lease_id=acquired.lease_id)]


async def test_waits_then_scrapes():
    h = Harness(slots=[BUSY, BUSY])
    assert await h.run() == SCRAPE_OUTPUT
    assert len(h.acquired) == 3
    # One lease id across the retries, so a retried acquire cannot leave a second lease.
    assert len({a.lease_id for a in h.acquired}) == 1
    assert len(h.released) == 1


async def test_wait_cap_scrapes_without_a_slot():
    minute = HostSlot(domain="shop.com", wait_ms=60_000)
    h = Harness(slots=[minute] * 100)
    assert await h.run() == SCRAPE_OUTPUT
    waits = HOST_WAIT_CAP // timedelta(minutes=1)
    assert len(h.acquired) == waits + 1
    assert h.scraped == 1
    # No lease was granted, so there is none to release.
    assert h.released == []


async def test_the_last_wait_stops_at_the_cap():
    # 7 min, then the 3 min left of the cap — not another 7.
    h = Harness(slots=[HostSlot(domain="shop.com", wait_ms=7 * 60_000)] * 10)
    assert await h.run() == SCRAPE_OUTPUT
    assert len(h.acquired) == 3
    assert h.asked_at[-1] - h.asked_at[0] < HOST_WAIT_CAP + timedelta(seconds=15)


async def test_reasks_back_off_to_a_few_dozen():
    # Busy for the whole cap: 1, 2, 4, 8, 16 s, then 30 s until 10 min.
    h = Harness(slots=[BUSY] * 1000)
    assert await h.run() == SCRAPE_OUTPUT
    assert 20 <= len(h.acquired) <= 30
    assert h.scraped == 1


async def test_acquire_activity_failure_goes():
    h = Harness(acquire_error=True)
    assert await h.run() == SCRAPE_OUTPUT
    assert len(h.acquired) == 2  # the activity's retry policy
    assert h.scraped == 1
    assert h.released == []


async def test_cancel_during_the_wait_ends_it_without_a_scrape():
    async def cancel(handle):
        await h.asked.wait()
        await handle.cancel()

    h = Harness(slots=[BUSY] * 1000)
    result = await h.run(during=cancel)
    assert isinstance(result, WorkflowFailureError)
    assert h.scraped == 0
    assert len(h.acquired) < 5
    assert h.released == []


async def test_cancel_during_a_granted_acquire_releases_the_lease():
    gate = asyncio.Event()

    async def cancel(handle):
        await h.asked.wait()
        await handle.cancel()
        gate.set()

    h = Harness(acquire_gate=gate)
    result = await h.run(during=cancel)
    assert isinstance(result, WorkflowFailureError)
    assert h.scraped == 0
    (acquired,) = h.acquired
    assert h.released == [HostSlotRelease(domain="shop.com", lease_id=acquired.lease_id)]


async def test_failed_scrape_releases():
    async def fail():
        raise ApplicationError("blocked:akamai", non_retryable=True)

    h = Harness(scrape=fail)
    result = await h.run()
    assert isinstance(result, WorkflowFailureError)
    assert len(h.released) == 1


async def test_cancelled_scrape_releases():
    started = asyncio.Event()

    async def hang():
        started.set()
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.1)

    async def cancel(handle):
        await started.wait()
        await handle.cancel()

    h = Harness(scrape=hang)
    result = await h.run(during=cancel)
    assert isinstance(result, WorkflowFailureError)
    assert len(h.released) == 1
