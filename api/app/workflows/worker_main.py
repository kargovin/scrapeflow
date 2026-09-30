import signal
import sys

if __name__ == "__main__":
    # Run as PID 1 (`python -m`, not `uv run` — uv as PID 1 ignores SIGTERM during its own startup):
    # PID 1 ignores a SIGTERM it has no handler for, and the imports below take seconds. Nothing is
    # in flight yet, so exit at once; main() replaces this with its graceful handler.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

import asyncio
from datetime import timedelta

import structlog
from temporalio.worker import Worker

from app.settings import settings
from app.workflows.client import connect
from app.workflows.hello import HelloWorkflow, say_hello
from app.workflows.probe import LLMProbeWorkflow, ScrapeProbeWorkflow
from app.workflows.queues import WORKFLOW_QUEUE

logger = structlog.get_logger()


async def main() -> None:
    # Before connect(): a SIGTERM during startup must not fall between the two handlers.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    client = await connect()
    worker = Worker(
        client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[HelloWorkflow, ScrapeProbeWorkflow, LLMProbeWorkflow],
        activities=[say_hello],
        graceful_shutdown_timeout=timedelta(seconds=20),
    )

    async with worker:
        logger.info(
            "Workflow worker started",
            address=settings.temporal_address,
            namespace=client.namespace,
            task_queue=WORKFLOW_QUEUE,
        )
        await stop.wait()
        logger.info("Workflow worker stopping")
    logger.info("Workflow worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
