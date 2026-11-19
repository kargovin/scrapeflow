"""Clean and Validate — the Temporal activities on the content queue."""

import asyncio
import hashlib

import structlog
from miniopy_async import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .cleaner import clean_html
from .contracts import (
    CLEAN_ACTIVITY,
    ChangedRule,
    VALIDATE_ACTIVITY,
    CleanInput,
    CleanOutput,
    StoredObject,
    ValidateInput,
    ValidateVerdict,
)
from .errors import TERMINAL, classify, describe
from .rules import NO_FINGERPRINTS, Fingerprints, failures, first_unmet, label
from .storage import fetch_bytes, put

log = structlog.get_logger()

# ApplicationError.type values.
CONTENT_FAILED = "ContentFailed"  # non-retryable
VALIDATION_FAILED = "ValidationFailed"  # non-retryable
VALIDATION_UNVERIFIABLE = "ValidationUnverifiable"  # non-retryable
STORAGE_TRANSIENT = "StorageTransient"


def _storage_error(exc: Exception) -> ApplicationError:
    if classify(exc) == TERMINAL:
        return ApplicationError(describe(exc), type=CONTENT_FAILED, non_retryable=True)
    return ApplicationError(describe(exc), type=STORAGE_TRANSIENT)


class ContentActivities:
    def __init__(self, minio: Minio) -> None:
        self._minio = minio

    async def _read_bytes(self, path: str) -> bytes:
        try:
            return await fetch_bytes(self._minio, path)
        except Exception as exc:
            raise _storage_error(exc) from exc

    async def _read(self, path: str) -> str:
        return (await self._read_bytes(path)).decode("utf-8", errors="replace")

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
    async def validate(self, job: ValidateInput) -> ValidateVerdict | None:
        """Fail mode: raise naming every rule not met. Stop mode: the first rule not met decides."""
        data = await self._read_bytes(job.input_path)
        # ⚠️ The bytes, before the decode: errors="replace" maps different bytes to one text.
        prints = NO_FINGERPRINTS
        if any(isinstance(r, ChangedRule) for r in job.rules):
            computed = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
            prints = Fingerprints(computed=computed, known=job.known_fingerprints)
        text = data.decode("utf-8", errors="replace")
        if job.when_unmet == "stop":
            return await self._validate_stop(job, text, prints)
        problems = await asyncio.to_thread(failures, job.rules, text, prints)
        if problems:
            log.info("validate_failed", path=job.input_path, problems=problems)
            raise ApplicationError(
                "; ".join(problems), type=VALIDATION_FAILED, non_retryable=True
            )
        log.info("validate_passed", path=job.input_path, rules=len(job.rules))
        return None

    async def _validate_stop(
        self, job: ValidateInput, text: str, prints: Fingerprints
    ) -> ValidateVerdict:
        fingerprint = prints.computed or None
        first = await asyncio.to_thread(first_unmet, job.rules, text, prints)
        if first is None:
            log.info("validate_passed", path=job.input_path, rules=len(job.rules))
            return ValidateVerdict(outcome="met", fingerprint=fingerprint)
        index, result = first
        rule = job.rules[index]
        if result.answer == "unverifiable":
            problem = f"{label(index, rule)}: {result.problem}"
            log.info("validate_unverifiable", path=job.input_path, problem=problem)
            raise ApplicationError(
                problem, type=VALIDATION_UNVERIFIABLE, non_retryable=True
            )
        log.info("validate_not_met", path=job.input_path, rule_index=index)
        return ValidateVerdict(
            outcome="not_met",
            rule_index=index,
            rule=rule.model_dump(mode="json", by_alias=True),
            found=result.found,
            expected=result.expected,
            fingerprint=fingerprint,
            matched=result.matched,
        )
