import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager

import asyncpg
import structlog

from app.core.pipeline_status import PIPELINE_STATUS_CHANNEL
from app.settings import settings

logger = structlog.get_logger()

_JOB_CHANNEL = "job_status"
_BATCH_CHANNEL = "batch_status"

# Seconds before each reconnect attempt; the last repeats until one succeeds.
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)
# A half-open TCP connection never terminates on its own; a ping finds it.
HEALTH_CHECK_SECONDS = 30
HEALTH_CHECK_TIMEOUT = 10


class WebSocketConnectionLimitExceeded(Exception):
    pass


class JobNotifier:
    """Singleton. One dedicated asyncpg LISTEN connection per API process.

    The dedicated connection must never be returned to the SQLAlchemy pool —
    a LISTEN connection holds its channel subscriptions for its lifetime.
    Started at API startup; stopped at shutdown.

    BUG-009: a dropped connection is reconnected on a backoff and every channel re-registered.
    pg_notify has no replay — an update sent while disconnected is lost; clients re-read the row
    when they reconnect (ADR-009 §11b).
    """

    def __init__(self) -> None:
        self._conn: asyncpg.Connection | None = None
        self._dsn: str | None = None
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        self._job_waiters: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._batch_waiters: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._pipeline_waiters: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._user_connection_counts: dict[str, int] = defaultdict(int)

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.is_closed()

    async def start(self, dsn: str) -> None:
        self._dsn = dsn
        self._stopping = False
        await self._connect()
        self._spawn(self._health_check())

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        if self._conn:
            await self._conn.close()
            self._conn = None

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _connect(self) -> None:
        conn = await asyncpg.connect(self._dsn)
        listeners = {
            _JOB_CHANNEL: self._on_job_notify,
            _BATCH_CHANNEL: self._on_batch_notify,
            PIPELINE_STATUS_CHANNEL: self._on_pipeline_notify,
        }
        for channel, callback in listeners.items():
            await conn.add_listener(channel, callback)
        conn.add_termination_listener(self._on_terminated)
        self._conn = conn
        logger.info("job_notifier_started", channels=list(listeners))

    def _on_terminated(self, conn: asyncpg.Connection) -> None:
        # stop() clears _conn before asyncpg calls this, so a deliberate close is ignored here.
        if conn is not self._conn:
            return
        self._conn = None
        logger.error(
            "job_notifier_connection_lost",
            detail="live status updates stop until the notifier reconnects",
            subscribers=sum(
                len(q)
                for w in (self._job_waiters, self._batch_waiters, self._pipeline_waiters)
                for q in w.values()
            ),
        )
        self._spawn(self._reconnect())

    async def _reconnect(self) -> None:
        attempt = 0
        while not self._stopping:
            await asyncio.sleep(RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)])
            attempt += 1
            try:
                await self._connect()
            except Exception as exc:
                logger.error("job_notifier_reconnect_failed", attempt=attempt, error=str(exc))
                continue
            logger.warning("job_notifier_reconnected", attempts=attempt)
            return

    async def _health_check(self) -> None:
        while not self._stopping:
            await asyncio.sleep(HEALTH_CHECK_SECONDS)
            conn = self._conn
            if conn is None or conn.is_closed():
                continue
            try:
                await asyncio.wait_for(conn.execute("SELECT 1"), HEALTH_CHECK_TIMEOUT)
            except Exception as exc:
                logger.error(
                    "job_notifier_health_check_failed", error=str(exc) or type(exc).__name__
                )
                # Fires the termination listener, which reconnects.
                conn.terminate()

    @staticmethod
    def _deliver(waiters: dict[str, list[asyncio.Queue]], key: str, update: dict) -> None:
        for queue in list(waiters.get(key, [])):
            try:
                queue.put_nowait(update)
            except asyncio.QueueFull:
                logger.warning("job_notifier: dropped update, subscriber queue full", key=key)

    async def _on_job_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        # payload: "job_id:run_id:status"  (UUIDs and status values contain no colons)
        try:
            job_id, run_id, new_status = payload.split(":")
        except ValueError:
            logger.warning("job_notifier: malformed job_status payload", payload=payload)
            return
        self._deliver(self._job_waiters, job_id, {"run_id": run_id, "status": new_status})

    async def _on_batch_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        # payload: JSON
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            logger.warning("job_notifier: malformed batch_status payload", payload=payload)
            return
        self._deliver(self._batch_waiters, data.get("batch_id", ""), data)

    async def _on_pipeline_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        # payload: JSON, core/pipeline_status.notify_run
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            logger.warning("job_notifier: malformed pipeline_status payload", payload=payload)
            return
        self._deliver(self._pipeline_waiters, data.get("run_id", ""), data)

    @asynccontextmanager
    async def _subscribe(self, waiters: dict[str, list[asyncio.Queue]], key: str, user_id: str):
        limit = settings.ws_max_connections_per_user
        if self._user_connection_counts[user_id] >= limit:
            raise WebSocketConnectionLimitExceeded(
                f"Maximum {limit} concurrent WebSocket connections exceeded"
            )
        self._user_connection_counts[user_id] += 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        waiters[key].append(queue)
        try:
            yield queue
        finally:
            try:
                waiters[key].remove(queue)
            except ValueError:
                pass
            if not waiters.get(key):
                waiters.pop(key, None)
            self._user_connection_counts[user_id] -= 1
            if not self._user_connection_counts[user_id]:
                self._user_connection_counts.pop(user_id, None)

    def subscribe_job(self, job_id: str, user_id: str):
        return self._subscribe(self._job_waiters, job_id, user_id)

    def subscribe_batch(self, batch_id: str, user_id: str):
        return self._subscribe(self._batch_waiters, batch_id, user_id)

    def subscribe_pipeline_run(self, run_id: str, user_id: str):
        return self._subscribe(self._pipeline_waiters, run_id, user_id)
