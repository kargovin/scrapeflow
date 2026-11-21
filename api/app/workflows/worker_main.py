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

from app.core import webhook_delivery
from app.settings import settings
from app.workflows.activities.approvals import (
    remind_approval,
    request_approval,
    resolve_approval,
    supersede_approval,
)
from app.workflows.activities.batches import finish_batch
from app.workflows.activities.crawls import (
    admit_urls,
    claim_crawl_pages,
    crawl_started,
    finish_crawl,
    record_crawl_page,
    vet_urls,
)
from app.workflows.activities.db import (
    check_cancelled,
    load_destination,
    mirror_block_status,
    mirror_rollback_started,
    mirror_run_status,
    mirror_undo_status,
    record_storage,
    release_run_outputs,
)
from app.workflows.activities.host_slots import acquire_host_slot, release_host_slot
from app.workflows.activities.job_runs import (
    finish_job_run,
    job_run_processing,
    job_run_started,
    read_llm_settings,
    record_job_objects,
)
from app.workflows.activities.monitor_checks import admit_monitor_check
from app.workflows.activities.schedules import admit_scheduled_run
from app.workflows.activities.webhook import deliver_webhook
from app.workflows.activities.webhook_deliveries import (
    attempt_webhook_delivery,
    record_email_attempt,
    sweep_pending_webhooks,
)
from app.workflows.batch import BatchWorkflow
from app.workflows.client import connect
from app.workflows.crawl import CrawlWorkflow
from app.workflows.destination_test import DestinationTestWorkflow
from app.workflows.hello import HelloWorkflow, say_hello
from app.workflows.job import JobWorkflow
from app.workflows.monitor_check import MonitorCheckWorkflow
from app.workflows.pipeline import PipelineWorkflow
from app.workflows.probe import LLMProbeWorkflow, ScrapeProbeWorkflow
from app.workflows.queues import WORKFLOW_QUEUE
from app.workflows.scheduled_job import ScheduledJobWorkflow
from app.workflows.webhook_delivery import WebhookDeliveryWorkflow, WebhookSweepWorkflow

logger = structlog.get_logger()


async def main() -> None:
    # Before connect(): a SIGTERM during startup must not fall between the two handlers.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    client = await connect()
    # The job lane's delivery backstop (F.4): a pending row nothing is delivering.
    await webhook_delivery.ensure_sweep(client)
    worker = Worker(
        client,
        task_queue=WORKFLOW_QUEUE,
        workflows=[
            HelloWorkflow,
            ScrapeProbeWorkflow,
            LLMProbeWorkflow,
            PipelineWorkflow,
            JobWorkflow,
            BatchWorkflow,
            CrawlWorkflow,
            ScheduledJobWorkflow,
            MonitorCheckWorkflow,
            DestinationTestWorkflow,
            WebhookDeliveryWorkflow,
            WebhookSweepWorkflow,
        ],
        activities=[
            say_hello,
            mirror_block_status,
            mirror_run_status,
            mirror_rollback_started,
            mirror_undo_status,
            record_storage,
            release_run_outputs,
            check_cancelled,
            load_destination,
            request_approval,
            remind_approval,
            resolve_approval,
            supersede_approval,
            deliver_webhook,
            job_run_started,
            read_llm_settings,
            record_job_objects,
            job_run_processing,
            finish_job_run,
            finish_batch,
            acquire_host_slot,
            release_host_slot,
            crawl_started,
            admit_urls,
            vet_urls,
            claim_crawl_pages,
            record_crawl_page,
            finish_crawl,
            admit_scheduled_run,
            admit_monitor_check,
            attempt_webhook_delivery,
            record_email_attempt,
            sweep_pending_webhooks,
        ],
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
