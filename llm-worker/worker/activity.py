"""
LLMExtract — the Temporal activity for the LLM stage (WORKER_MODE=temporal).

Same steps as worker.handle_message minus the transport: no "running" publish, no
result publish, no ack/nak. Success is the return value; failure is a raised
ApplicationError, and the caller's RetryPolicy decides when to stop.
"""

import asyncio
import contextlib
import json

import structlog
from miniopy_async import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import settings
from .contracts import LLM_EXTRACT_ACTIVITY, LLMInput, LLMOutput, StoredObject
from .errors import TERMINAL, classify, describe
from .llm import call_llm
from .storage import fetch_content, upload

log = structlog.get_logger()

# ApplicationError.type values.
LLM_FAILED = "LLMFailed"  # non-retryable
LLM_TRANSIENT = "LLMTransient"
STORAGE_TRANSIENT = "StorageTransient"


def to_application_error(exc: Exception, stage: str) -> ApplicationError:
    # The SDK retries any exception that is not an ApplicationError, so every failure must
    # pass through classify() or the fail-closed default is lost.
    detail = describe(exc)
    if classify(exc) == TERMINAL:
        return ApplicationError(detail, type=LLM_FAILED, non_retryable=True)
    return ApplicationError(
        detail, type=LLM_TRANSIENT if stage == "llm" else STORAGE_TRANSIENT
    )


async def _heartbeat() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(settings.llm_heartbeat_seconds)


class LLMActivities:
    def __init__(self, minio: Minio) -> None:
        self._minio = minio

    @activity.defn(name=LLM_EXTRACT_ACTIVITY)
    async def llm_extract(self, job: LLMInput) -> LLMOutput:
        """Read the scrape, call the user's LLM, write history/{artifact_id}/llm.json.

        Caller requirements:
          - start_to_close >= llm_warmup_max_wait_seconds + llm_request_timeout_seconds:
            180 + 180 = 360 s against production (LLM_REQUEST_TIMEOUT_SECONDS=180 in the
            infra repo), not the 240 s the repo defaults give.
          - heartbeat_timeout > llm_heartbeat_seconds (30 s). Heartbeats run through
            warm-up and the call.
          - RetryPolicy: 5 s initial, x2, 60 s max, 3 attempts — never the unlimited default.
        """
        attempt = activity.info().attempt
        log.info(
            "job_received",
            artifact_id=job.artifact_id,
            model=job.model,
            attempt=attempt,
        )

        hb = asyncio.create_task(_heartbeat())
        stage = "fetch"
        try:
            content = await fetch_content(self._minio, job.raw_minio_path)

            stage = "llm"
            result_dict = await call_llm(
                encrypted_api_key=job.encrypted_api_key,
                provider=job.provider,
                base_url=job.base_url,
                model=job.model,
                content=content,
                output_schema=job.output_schema,
            )

            stage = "upload"
            result_bytes = json.dumps(result_dict).encode()
            minio_path = await upload(self._minio, job.artifact_id, result_bytes)
        except Exception as exc:
            # CancelledError is a BaseException and passes through: it is how a cancel or a
            # shutdown past the grace period reaches the activity.
            err = to_application_error(exc, stage)
            log_fn = log.error if err.non_retryable else log.warning
            log_fn(
                "job_failed" if err.non_retryable else "job_transient_failure",
                artifact_id=job.artifact_id,
                error=err.message,
                type=err.type,
                stage=stage,
                attempt=attempt,
            )
            raise err from exc
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

        log.info("job_completed", artifact_id=job.artifact_id, path=minio_path)
        return LLMOutput(result=StoredObject(path=minio_path, size=len(result_bytes)))
