import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager

import asyncpg
import structlog

from app.settings import settings

logger = structlog.get_logger()

_JOB_CHANNEL = "job_status"
_BATCH_CHANNEL = "batch_status"


class WebSocketConnectionLimitExceeded(Exception):
    pass


class JobNotifier:
    """Singleton. One dedicated asyncpg LISTEN connection per API process.

    The dedicated connection must never be returned to the SQLAlchemy pool —
    a LISTEN connection holds its channel subscriptions for its lifetime.
    Started at API startup; stopped at shutdown.
    """

    def __init__(self) -> None:
        self._conn: asyncpg.Connection | None = None
        self._job_waiters: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._batch_waiters: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._user_connection_counts: dict[str, int] = defaultdict(int)

    async def start(self, dsn: str) -> None:
        self._conn = await asyncpg.connect(dsn)
        await self._conn.add_listener(_JOB_CHANNEL, self._on_job_notify)
        await self._conn.add_listener(_BATCH_CHANNEL, self._on_batch_notify)
        logger.info("job_notifier_started", channels=[_JOB_CHANNEL, _BATCH_CHANNEL])

    async def stop(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _on_job_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        # payload: "job_id:run_id:status"  (UUIDs and status values contain no colons)
        try:
            job_id, run_id, new_status = payload.split(":")
        except ValueError:
            logger.warning("job_notifier: malformed job_status payload", payload=payload)
            return
        for queue in list(self._job_waiters.get(job_id, [])):
            try:
                queue.put_nowait({"run_id": run_id, "status": new_status})
            except asyncio.QueueFull:
                logger.warning("job_notifier: dropped update, subscriber queue full", job_id=job_id)

    async def _on_batch_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        # payload: JSON
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            logger.warning("job_notifier: malformed batch_status payload", payload=payload)
            return
        batch_id = data.get("batch_id", "")
        for queue in list(self._batch_waiters.get(batch_id, [])):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                logger.warning(
                    "job_notifier: dropped update, subscriber queue full", batch_id=batch_id
                )

    @asynccontextmanager
    async def subscribe_job(self, job_id: str, user_id: str):
        limit = settings.ws_max_connections_per_user
        if self._user_connection_counts[user_id] >= limit:
            raise WebSocketConnectionLimitExceeded(
                f"Maximum {limit} concurrent WebSocket connections exceeded"
            )
        self._user_connection_counts[user_id] += 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._job_waiters[job_id].append(queue)
        try:
            yield queue
        finally:
            try:
                self._job_waiters[job_id].remove(queue)
            except ValueError:
                pass
            if not self._job_waiters.get(job_id):
                self._job_waiters.pop(job_id, None)
            self._user_connection_counts[user_id] -= 1
            if not self._user_connection_counts[user_id]:
                self._user_connection_counts.pop(user_id, None)

    @asynccontextmanager
    async def subscribe_batch(self, batch_id: str, user_id: str):
        limit = settings.ws_max_connections_per_user
        if self._user_connection_counts[user_id] >= limit:
            raise WebSocketConnectionLimitExceeded(
                f"Maximum {limit} concurrent WebSocket connections exceeded"
            )
        self._user_connection_counts[user_id] += 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._batch_waiters[batch_id].append(queue)
        try:
            yield queue
        finally:
            try:
                self._batch_waiters[batch_id].remove(queue)
            except ValueError:
                pass
            if not self._batch_waiters.get(batch_id):
                self._batch_waiters.pop(batch_id, None)
            self._user_connection_counts[user_id] -= 1
            if not self._user_connection_counts[user_id]:
                self._user_connection_counts.pop(user_id, None)
