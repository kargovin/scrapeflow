"""Clean and Validate — the Temporal activities on the content queue."""

import asyncio

import structlog
from miniopy_async import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .cleaner import clean_html
from .contracts import (
    CLEAN_ACTIVITY,
    VALIDATE_ACTIVITY,
    CleanInput,
    CleanOutput,
    StoredObject,
    ValidateInput,
)
from .errors import TERMINAL, classify, describe
from .rules import failures
from .storage import fetch_bytes, put

log = structlog.get_logger()

# ApplicationError.type values.
CONTENT_FAILED = "ContentFailed"  # non-retryable
VALIDATION_FAILED = "ValidationFailed"  # non-retryable
STORAGE_TRANSIENT = "StorageTransient"


def _storage_error(exc: Exception) -> ApplicationError:
    if classify(exc) == TERMINAL:
        return ApplicationError(describe(exc), type=CONTENT_FAILED, non_retryable=True)
    return ApplicationError(describe(exc), type=STORAGE_TRANSIENT)


class ContentActivities:
    def __init__(self, minio: Minio) -> None:
        self._minio = minio

    async def _read(self, path: str) -> str:
        try:
            data = await fetch_bytes(self._minio, path)
        except Exception as exc:
            raise _storage_error(exc) from exc
        return data.decode("utf-8", errors="replace")

    @activity.defn(name=CLEAN_ACTIVITY)
    async def clean(self, job: CleanInput) -> CleanOutput:
        """Strip boilerplate from an HTML page; write history/{artifact_id}/clean.md."""
        html = await self._read(job.input_path)
        try:
            # Off the event loop: the LLM activities in this process must keep heartbeating.
            markdown = (await asyncio.to_thread(clean_html, html)).encode()
        except Exception as exc:
            raise ApplicationError(
                describe(exc), type=CONTENT_FAILED, non_retryable=True
            ) from exc
        try:
            path = await put(
                self._minio,
                f"history/{job.artifact_id}/clean.md",
                markdown,
                "text/markdown; charset=utf-8",
            )
        except Exception as exc:
            raise _storage_error(exc) from exc
        log.info(
            "clean_completed",
            artifact_id=job.artifact_id,
            path=path,
            bytes_in=len(html),
            bytes_out=len(markdown),
        )
        return CleanOutput(result=StoredObject(path=path, size=len(markdown)))

    @activity.defn(name=VALIDATE_ACTIVITY)
    async def validate(self, job: ValidateInput) -> None:
        """Check the input object against every rule; raise naming each one that fails."""
        text = await self._read(job.input_path)
        problems = await asyncio.to_thread(failures, job.rules, text)
        if problems:
            log.info("validate_failed", path=job.input_path, problems=problems)
            raise ApplicationError(
                "; ".join(problems), type=VALIDATION_FAILED, non_retryable=True
            )
        log.info("validate_passed", path=job.input_path, rules=len(job.rules))
