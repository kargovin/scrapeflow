"""C.9 — the Webhook activity: the wire contract against a real receiver, and its mirror writes."""

import asyncio
import dataclasses
import hashlib
import hmac
import json
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.core.db import AsyncSessionLocal
from app.models.pipeline import PipelineRun, PipelineRunBlock
from app.settings import settings
from app.workflows.activities import webhook as webhook_module
from app.workflows.activities.webhook import deliver_webhook
from app.workflows.pipeline import RUN_CANCELLED_ERROR, WebhookInput
from tests.test_workflows_pipeline import LLM_PATH, _block, _run_rows

HOOK_BLOCKS = (("extract", "llm_extract", "completed"), ("notify", "webhook", "running"))


class Receiver:
    """A bare HTTP/1.1 endpoint on 127.0.0.1 that records each request's headers and raw body."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[tuple[dict[str, str], bytes]] = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        lines = [line.split(": ", 1) for line in head.split("\r\n")[1:] if line]
        headers = {k.lower(): v for k, v in lines}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append((headers, body))
        writer.write(
            f"HTTP/1.1 {self.status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> "Receiver":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/hook"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
def no_ssrf(monkeypatch):
    # The receiver is on loopback, which the real check refuses (that path has its own test).
    monkeypatch.setattr(webhook_module, "validate_no_ssrf_core", lambda url: None)


def _input(run_id, url, secret: str | None = None, **fields) -> WebhookInput:
    encrypted = (
        Fernet(settings.credentials_encryption_key).encrypt(secret.encode()).decode()
        if secret
        else None
    )
    return WebhookInput(
        run_id=str(run_id),
        pipeline_id=str(fields.pop("pipeline_id", uuid.uuid4())),
        block_id="notify",
        url=url,
        result_path=LLM_PATH,
        encrypted_secret=encrypted,
        timestamp="2026-10-11T12:00:00+00:00",
        maximum_attempts=5,
        **fields,
    )


async def _attempt(input: WebhookInput, attempt: int = 1):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return await env.run(deliver_webhook, input)


async def test_the_wire_contract_is_the_job_lanes(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver() as receiver:
        await _attempt(_input(run.id, receiver.url, secret="s3cret", pipeline_id=run.pipeline_id))

    [(headers, body)] = receiver.requests
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["x-scrapeflow-signature"] == f"sha256={expected}"
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {
        "event": "pipeline.result",
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "pipeline_run_id": str(run.id),
        "result_path": LLM_PATH,
        "diff_detected": False,
        "diff_summary": None,
        "timestamp": "2026-10-11T12:00:00+00:00",
    }


async def test_no_secret_still_sends_the_header_signed_with_empty_bytes(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver() as receiver:
        await _attempt(_input(run.id, receiver.url))

    [(headers, body)] = receiver.requests
    assert headers["x-scrapeflow-signature"] == (
        "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    )


@pytest.mark.parametrize("code", [200, 204, 299])
async def test_any_status_below_300_is_delivered(db_user, no_ssrf, code):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver(code) as receiver:
        await _attempt(_input(run.id, receiver.url))
    assert (await _block(run.id, "notify")).status == "running"


@pytest.mark.parametrize("code", [300, 404, 503])
async def test_300_and_up_fails_the_attempt_and_the_block_waits(db_user, no_ssrf, code):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver(code) as receiver:
        with pytest.raises(ApplicationError) as exc:
            await _attempt(_input(run.id, receiver.url), attempt=2)

    # Retryable: a 4xx is retried too, as on the job lane.
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        "WebhookFailed",
        False,
        f"HTTP {code}",
    )
    block = await _block(run.id, "notify")
    assert (block.status, block.error) == ("waiting", f"attempt 2: HTTP {code}")


async def test_the_last_attempt_leaves_no_waiting_block(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver(503) as receiver:
        with pytest.raises(ApplicationError):
            await _attempt(_input(run.id, receiver.url), attempt=5)
    assert (await _block(run.id, "notify")).status == "running"


async def test_an_unreachable_receiver_is_a_retryable_failure(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver() as receiver:
        pass  # closed: connection refused
    with pytest.raises(ApplicationError) as exc:
        await _attempt(_input(run.id, receiver.url))
    assert (exc.value.type, exc.value.non_retryable) == ("WebhookFailed", False)
    assert exc.value.message.startswith("ConnectError")
    assert (await _block(run.id, "notify")).status == "waiting"


async def test_an_ssrf_refusal_is_final_and_sends_nothing(db_user):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver() as receiver:  # 127.0.0.1 — the real check refuses it
        with pytest.raises(ApplicationError) as exc:
            await _attempt(_input(run.id, receiver.url))

    assert receiver.requests == []
    assert (exc.value.type, exc.value.non_retryable) == ("WebhookBlocked", True)
    assert exc.value.message.startswith("ssrf_blocked: ")
    assert (await _block(run.id, "notify")).status == "running"


async def test_a_retry_on_a_cancel_requested_run_stops_without_posting(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id)
            .values(cancel_requested_at=run.created_at)
        )
        await db.commit()

    async with Receiver() as receiver:
        with pytest.raises(ApplicationError) as exc:
            await _attempt(_input(run.id, receiver.url), attempt=2)

    assert receiver.requests == []
    assert (exc.value.type, exc.value.non_retryable) == (RUN_CANCELLED_ERROR, True)


async def test_a_retry_flips_waiting_back_to_running_and_keeps_the_first_start(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver(503) as receiver:
        with pytest.raises(ApplicationError):
            await _attempt(_input(run.id, receiver.url), attempt=1)
    first = await _block(run.id, "notify")
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunBlock)
            .where(PipelineRunBlock.id == first.id)
            .values(started_at=run.created_at)
        )
        await db.commit()

    seen = []
    real_post = webhook_module.httpx.AsyncClient.post

    async def post_and_look(self, *args, **kwargs):
        seen.append(await _block(run.id, "notify"))
        return await real_post(self, *args, **kwargs)

    async with Receiver(200) as receiver:
        webhook_module.httpx.AsyncClient.post = post_and_look
        try:
            await _attempt(_input(run.id, receiver.url), attempt=2)
        finally:
            webhook_module.httpx.AsyncClient.post = real_post

    [during] = seen
    assert (during.status, during.error, during.started_at) == ("running", None, run.created_at)


async def test_an_undecryptable_secret_is_final(db_user, no_ssrf):
    run = await _run_rows(db_user.id, HOOK_BLOCKS)
    async with Receiver() as receiver:
        with pytest.raises(ApplicationError) as exc:
            await _attempt(
                _input(run.id, receiver.url).model_copy(update={"encrypted_secret": "nope"})
            )
    assert receiver.requests == []
    assert (exc.value.type, exc.value.non_retryable) == ("WebhookSecret", True)
