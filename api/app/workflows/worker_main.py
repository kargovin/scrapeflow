import asyncio
import signal
from datetime import timedelta

import structlog
from temporalio.worker import Worker

from app.settings import settings
from app.workflows.client import connect
from app.workflows.hello import HelloWorkflow, say_hello
from app.workflows.queues import WORKFLOW_QUEUE

logger = structlog.get_logger()


async def main() -> None:
    client = await connect()
    worker = Worker(
        client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[HelloWorkflow],
        activities=[say_hello],
        graceful_shutdown_timeout=timedelta(seconds=20),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

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
