"""M.14 — a ``kind = 'email'`` row of ``webhook_deliveries``: the row read on the workflow queue,
SendEmail on the delivery queue, the outcome written back on the row's backoff (ADR-015 §4, §5)."""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import delete
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from app.core.db import AsyncSessionLocal
from app.core.destinations import encrypt_secret
from app.core.email_deliveries import email_for
from app.core.pipeline_alerts import failed_payload
from app.models.destination import Destination
from app.models.pipeline import Pipeline
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.contracts import (
    EMAIL_SEND_ATTEMPT_SECONDS,
    SEND_EMAIL_ACTIVITY,
    SendEmailInput,
    SendEmailOutput,
)
from app.workflows.activities.webhook_deliveries import (
    BACKOFF_SECONDS,
    attempt_webhook_delivery,
    record_email_attempt,
)
from app.workflows.queues import DELIVERY_QUEUE, WORKFLOW_QUEUE
from app.workflows.webhook_delivery import (
    ATTEMPT_WEBHOOK_ACTIVITY,
    EMAIL_DEFERRED_PATCH,
    EMAIL_PATCH,
    RECORD_EMAIL_ATTEMPT_ACTIVITY,
    AttemptOutcome,
    DeliveryRef,
    EmailAttempt,
    WebhookDeliveryWorkflow,
    _failed_send,
)
from tests.test_pipeline_alerts import _run
from tests.test_webhook_deliveries import Receiver, stand_in

SMTP_ADDRESS = {
    "host": "mail.example.com",
    "port": 587,
    "tls_mode": "starttls",
    "from": "alerts@example.org",
    "to": ["ops@example.org"],
    "ca_certificate": None,
}


async def _destination(user_id, kind="smtp") -> Destination:
    async with AsyncSessionLocal() as db:
        d = Destination(
            user_id=user_id,
            name=f"mail-{uuid.uuid4().hex[:6]}",
            kind=kind,
            address=SMTP_ADDRESS if kind == "smtp" else {},
            encrypted_secret=encrypt_secret({"user": "u", "password": "p"}),
            display_host="mail.example.com",
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


async def _email_row(run, destination, **fields) -> WebhookDelivery:
    async with AsyncSessionLocal() as db:
        d = WebhookDelivery(
            pipeline_run_id=run.id,
            block_id="notify",
            kind="email",
            destination_id=destination.id if destination else None,
            event="pipeline.failed",
            payload=failed_payload(run, "notify"),
            **{"status": "pending", **fields},
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
    return d


async def _row(delivery_id) -> WebhookDelivery:
    async with AsyncSessionLocal() as db:
        return await db.get(WebhookDelivery, delivery_id)


async def _attempt(row) -> AttemptOutcome:
    return await ActivityEnvironment().run(
        attempt_webhook_delivery, DeliveryRef(delivery_id=str(row.id))
    )


async def _record(row, **fields) -> AttemptOutcome:
    return await ActivityEnvironment().run(
        record_email_attempt, EmailAttempt(delivery_id=str(row.id), **fields)
    )


@pytest.fixture
def receiver(monkeypatch):
    return stand_in(monkeypatch, Receiver())


# --- the attempt on the workflow queue: the message, never a POST ------------------------------


async def test_an_email_row_hands_the_workflow_its_message_and_sends_nothing(db_user, receiver):
    run = await _run(db_user.id, status="failed", error="fetch: HTTP 503")
    destination = await _destination(db_user.id)
    row = await _email_row(run, destination)

    outcome = await _attempt(row)

    assert outcome.status == "email"
    email = outcome.email
    assert email.message_id == f"<{run.id}.notify.failed@{settings.public_host}>"
    assert email.address == SMTP_ADDRESS
    assert email.encrypted_secret == destination.encrypted_secret
    assert email.run_id == str(run.id)
    async with AsyncSessionLocal() as db:
        name = (await db.get(Pipeline, run.pipeline_id)).name
    assert email.subject == f"[ScrapeFlow] {name}: pipeline.failed"
    assert email.body.startswith(f"pipeline.failed — pipeline {name}, run {run.id}\n\n")
    assert '"error": "fetch: HTTP 503"' in email.body
    assert receiver.posts == []
    after = await _row(row.id)
    assert (after.status, after.attempts, after.last_error) == ("pending", 0, None)


async def test_every_attempt_carries_the_same_message_id_and_the_current_destination(db_user):
    run = await _run(db_user.id, status="failed")
    destination = await _destination(db_user.id)
    row = await _email_row(run, destination)

    first = await _attempt(row)
    async with AsyncSessionLocal() as db:
        d = await db.get(Destination, destination.id)
        d.address = {**SMTP_ADDRESS, "to": ["new@example.org"]}
        await db.commit()
    second = await _attempt(row)

    assert first.email.message_id == second.email.message_id
    assert second.email.address["to"] == ["new@example.org"]


async def test_the_message_id_host_is_the_public_base_url_s(db_user, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://scrapeflow.example.org")
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))
    outcome = await _attempt(row)
    assert outcome.email.message_id == f"<{run.id}.notify.failed@scrapeflow.example.org>"


async def test_a_deleted_destination_gives_up_at_once(db_user):
    run = await _run(db_user.id, status="failed")
    destination = await _destination(db_user.id)
    row = await _email_row(run, destination)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Destination).where(Destination.id == destination.id))
        await db.commit()

    assert (await _attempt(row)).status == "exhausted"
    after = await _row(row.id)
    assert (after.status, after.attempts, after.last_error, after.destination_id) == (
        "exhausted",
        0,
        "destination deleted",
        None,
    )


async def test_a_destination_of_another_kind_gives_up(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id, kind="webhook"))
    assert (await _attempt(row)).status == "exhausted"
    after = await _row(row.id)
    assert after.last_error == "destination is a webhook destination, not smtp"


async def test_another_user_s_destination_is_never_used(db_user, other_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(other_user.id))
    assert (await _attempt(row)).status == "exhausted"
    assert (await _row(row.id)).last_error == "destination deleted"


async def test_an_email_row_for_an_owner_with_no_arm_raises():
    async with AsyncSessionLocal() as db:
        row = WebhookDelivery(
            id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            kind="email",
            event="job.failed",
        )
        with pytest.raises(ApplicationError) as exc:
            await email_for(db, row)
    assert exc.value.non_retryable


async def test_a_delivered_row_is_gone_to_a_late_attempt(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id), status="delivered")
    assert (await _attempt(row)).status == "gone"


# --- the outcome, written back on the row's schedule -------------------------------------------


async def test_a_sent_email_is_delivered(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))

    assert (await _record(row)).status == "delivered"
    after = await _row(row.id)
    assert (after.status, after.attempts, after.last_error) == ("delivered", 1, None)
    assert after.delivered_at is not None


async def test_recipients_the_server_refused_are_noted_on_a_delivered_row(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))
    await _record(row, refused=["a@example.org", "b@example.org"])
    after = await _row(row.id)
    assert (after.status, after.last_error) == (
        "delivered",
        "recipients refused: a@example.org, b@example.org",
    )


async def test_recipients_deferred_while_others_got_it_wait_on_the_backoff(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))

    outcome = await _record(row, deferred=["slow@example.org"])

    assert outcome.status == "retry"
    after = await _row(row.id)
    assert (after.status, after.attempts, after.last_error) == (
        "pending",
        1,
        "recipients deferred: slow@example.org",
    )


async def test_owed_recipients_on_the_last_attempt_leave_the_row_delivered_naming_them(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(
        run, await _destination(db_user.id), attempts=settings.webhook_max_attempts - 1
    )

    outcome = await _record(row, refused=["gone@example.org"], deferred=["slow@example.org"])

    assert outcome.status == "delivered"
    after = await _row(row.id)
    assert after.last_error == (
        "recipients refused: gone@example.org; recipients deferred, not sent: slow@example.org "
        f"(gave up after {settings.webhook_max_attempts} attempts)"
    )
    assert after.delivered_at is not None


async def test_owed_recipients_and_a_final_failure_leave_the_row_delivered(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))

    outcome = await _record(
        row,
        error="mail.example.com refused every recipient (550)",
        error_type="DestinationRefused",
        final=True,
        deferred=["slow@example.org"],
    )

    assert outcome.status == "delivered"
    assert (await _row(row.id)).last_error == (
        "recipients deferred, not sent: slow@example.org "
        "(mail.example.com refused every recipient (550))"
    )


async def test_a_transient_failure_waits_on_the_backoff(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id), attempts=1)

    before = datetime.now(UTC)
    outcome = await _record(
        row, error="mail.example.com deferred the sender (451)", error_type="DestinationUnavailable"
    )

    assert outcome.status == "retry"
    assert BACKOFF_SECONDS[2] - 2 <= outcome.retry_in_seconds <= BACKOFF_SECONDS[2]
    after = await _row(row.id)
    assert (after.status, after.attempts) == ("pending", 2)
    assert after.last_error == "mail.example.com deferred the sender (451)"
    assert after.next_attempt_at >= before + timedelta(seconds=BACKOFF_SECONDS[2] - 2)


async def test_a_transient_failure_on_the_last_attempt_gives_up(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(
        run, await _destination(db_user.id), attempts=settings.webhook_max_attempts - 1
    )
    outcome = await _record(row, error="timed out", error_type="DestinationUnreachable")
    assert outcome.status == "exhausted"


async def test_a_final_failure_gives_up_at_once(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))
    outcome = await _record(
        row,
        error="mail.example.com refused the login (535)",
        error_type="DestinationRefused",
        final=True,
    )
    assert outcome.status == "exhausted"
    after = await _row(row.id)
    assert (after.attempts, after.last_error) == (1, "mail.example.com refused the login (535)")


async def test_a_private_mail_host_is_a_security_event_not_an_attempt(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id))
    await _record(
        row,
        error="URL resolves to a private address: 10.0.0.7",
        error_type="EgressBlocked",
        final=True,
    )
    after = await _row(row.id)
    assert (after.status, after.attempts, after.last_error) == (
        "exhausted",
        0,
        "ssrf_blocked: URL resolves to a private address: 10.0.0.7",
    )


async def test_an_outcome_for_a_row_no_longer_pending_changes_nothing(db_user):
    run = await _run(db_user.id, status="failed")
    row = await _email_row(run, await _destination(db_user.id), status="exhausted", attempts=5)
    assert (await _record(row)).status == "gone"
    assert (await _row(row.id)).status == "exhausted"


# --- the workflow: SendEmail on the delivery queue, one try per attempt -------------------------

EMAIL = SendEmailInput(
    address=SMTP_ADDRESS,
    encrypted_secret="gAAAAAB_secret",
    message_id="<r.notify.failed@scrapeflow.example.org>",
    subject="[ScrapeFlow] p: pipeline.failed",
    body="pipeline.failed",
    run_id="r",
)


def _transient(_):
    raise ApplicationError(
        "mail.example.com deferred the sender (451)", type="DestinationUnavailable"
    )


def _final(_):
    raise ApplicationError(
        "mail.example.com refused the login (535)", type="DestinationRefused", non_retryable=True
    )


class Harness:
    """The workflow over a stand-in row: the attempt hands out EMAIL while the row is pending;
    ``record`` applies the row's backoff; ``sends`` play in turn (a callable raises)."""

    def __init__(self, sends, *, max_attempts=5):
        self.sends = list(sends)
        self.max_attempts = max_attempts
        self.attempts = 0
        self.status = "pending"
        self.sent: list[tuple[SendEmailInput, object]] = []
        self.recorded: list[EmailAttempt] = []
        self.asked: list[object] = []

    async def run(self) -> str:
        env_box = {}

        @activity.defn(name=ATTEMPT_WEBHOOK_ACTIVITY)
        async def attempt(input: DeliveryRef) -> AttemptOutcome:
            self.asked.append(await env_box["env"].get_current_time())
            if self.status != "pending":
                return AttemptOutcome(status="gone")
            return AttemptOutcome(status="email", email=EMAIL)

        @activity.defn(name=SEND_EMAIL_ACTIVITY)
        async def send(input: SendEmailInput) -> SendEmailOutput:
            self.sent.append((input, activity.info().attempt))
            play = self.sends.pop(0)
            if callable(play):
                play(input)
            return play

        @activity.defn(name=RECORD_EMAIL_ATTEMPT_ACTIVITY)
        async def record(input: EmailAttempt) -> AttemptOutcome:
            self.recorded.append(input)
            if input.error_type == "EgressBlocked":
                self.status = "exhausted"
                return AttemptOutcome(status="exhausted")
            self.attempts += 1
            last = input.final or self.attempts >= self.max_attempts
            if input.error is None and not input.deferred:
                self.status = "delivered"
            elif input.deferred and last:
                self.status = "delivered"
            elif last:
                self.status = "exhausted"
            else:
                return AttemptOutcome(
                    status="retry", retry_in_seconds=BACKOFF_SECONDS[self.attempts]
                )
            return AttemptOutcome(status=self.status)

        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            env_box["env"] = env
            async with Worker(
                env.client,
                task_queue=WORKFLOW_QUEUE,
                workflows=[WebhookDeliveryWorkflow],
                activities=[attempt, record],
            ):
                async with Worker(env.client, task_queue=DELIVERY_QUEUE, activities=[send]):
                    handle = await env.client.start_workflow(
                        WebhookDeliveryWorkflow.run,
                        DeliveryRef(delivery_id=str(uuid.uuid4())),
                        id=f"webhook-delivery-{uuid.uuid4()}",
                        task_queue=WORKFLOW_QUEUE,
                    )
                    result = await handle.result()
                    self.history = await handle.fetch_history()
                    return result


async def test_an_email_is_sent_on_the_delivery_queue_and_recorded():
    h = Harness([SendEmailOutput()])
    assert await h.run() == "delivered"

    [(sent, try_number)] = h.sent
    assert sent == EMAIL and try_number == 1
    assert h.recorded == [EmailAttempt(delivery_id=h.recorded[0].delivery_id)]
    [marker] = [
        e.marker_recorded_event_attributes
        for e in h.history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert EMAIL_PATCH.encode() in marker.SerializeToString()
    [scheduled] = [
        e.activity_task_scheduled_event_attributes
        for e in h.history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == SEND_EMAIL_ACTIVITY
    ]
    assert scheduled.task_queue.name == DELIVERY_QUEUE
    assert scheduled.start_to_close_timeout.seconds == EMAIL_SEND_ATTEMPT_SECONDS
    assert scheduled.retry_policy.maximum_attempts == 1


async def test_a_transient_send_is_tried_again_after_the_row_s_backoff_with_the_same_message():
    h = Harness([_transient, _transient, SendEmailOutput(refused=["b@example.org"])])
    assert await h.run() == "delivered"

    assert [s.message_id for s, _ in h.sent] == [EMAIL.message_id] * 3
    assert [n for _, n in h.sent] == [1, 1, 1]  # the workflow is the one retry layer
    assert [(r.error_type, r.final) for r in h.recorded] == [
        ("DestinationUnavailable", False),
        ("DestinationUnavailable", False),
        (None, False),
    ]
    assert h.recorded[-1].refused == ["b@example.org"]
    gaps = [(b - a).total_seconds() for a, b in pairwise(h.asked)]
    assert BACKOFF_SECONDS[1] <= gaps[0] < BACKOFF_SECONDS[1] + 5
    assert BACKOFF_SECONDS[2] <= gaps[1] < BACKOFF_SECONDS[2] + 5


async def test_a_final_send_gives_up_without_another_try():
    h = Harness([_final, SendEmailOutput()])
    assert await h.run() == "exhausted"
    assert len(h.sent) == 1
    [recorded] = h.recorded
    assert (recorded.error, recorded.error_type, recorded.final) == (
        "mail.example.com refused the login (535)",
        "DestinationRefused",
        True,
    )


async def test_a_later_send_goes_to_the_deferred_recipients_only():
    h = Harness(
        [
            SendEmailOutput(refused=["gone@example.org"], deferred=["slow@example.org"]),
            _transient,
            SendEmailOutput(),
        ]
    )
    assert await h.run() == "delivered"

    assert [s.recipients for s, _ in h.sent] == [None, ["slow@example.org"], ["slow@example.org"]]
    assert [(r.refused, r.deferred) for r in h.recorded] == [
        (["gone@example.org"], ["slow@example.org"]),
        (["gone@example.org"], ["slow@example.org"]),
        (["gone@example.org"], []),
    ]
    markers = [
        e.marker_recorded_event_attributes.SerializeToString()
        for e in h.history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert any(EMAIL_DEFERRED_PATCH.encode() in m for m in markers)


async def test_owed_recipients_and_a_final_send_end_delivered():
    h = Harness([SendEmailOutput(deferred=["slow@example.org"]), _final])
    assert await h.run() == "delivered"
    assert h.recorded[-1].deferred == ["slow@example.org"] and h.recorded[-1].final


async def test_transient_sends_stop_at_the_attempt_cap():
    h = Harness([_transient] * 5)
    assert await h.run() == "exhausted"
    assert len(h.sent) == 5


# The test server does not expire schedule_to_start; compose checks it live (M.14's status row).
@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            ApplicationError(
                "x refused the login (535)", type="DestinationRefused", non_retryable=True
            ),
            ("x refused the login (535)", "DestinationRefused", True),
        ),
        (
            ApplicationError("x deferred the sender (451)", type="DestinationUnavailable"),
            ("x deferred the sender (451)", "DestinationUnavailable", False),
        ),
        (
            ActivityTimeoutError(
                "t", type=TimeoutType.SCHEDULE_TO_START, last_heartbeat_details=[]
            ),
            ("no delivery worker took the send", "DeliveryTimeout", False),
        ),
        (
            ActivityTimeoutError("t", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            ("the send did not finish in time", "DeliveryTimeout", False),
        ),
        (CancelledError("c"), ("the send failed (CancelledError)", "DeliveryFailed", True)),
    ],
)
def test_how_a_failed_send_is_recorded(cause, expected):
    attempt = _failed_send(DeliveryRef(delivery_id="d"), cause)
    assert (attempt.error, attempt.error_type, attempt.final) == expected


async def test_a_webhook_row_s_attempt_is_unchanged_and_records_no_patch():
    outcomes = [AttemptOutcome(status="delivered")]

    @activity.defn(name=ATTEMPT_WEBHOOK_ACTIVITY)
    async def attempt(input: DeliveryRef) -> AttemptOutcome:
        return outcomes.pop(0)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[WebhookDeliveryWorkflow],
            activities=[attempt],
        ):
            handle = await env.client.start_workflow(
                WebhookDeliveryWorkflow.run,
                DeliveryRef(delivery_id=str(uuid.uuid4())),
                id=f"webhook-delivery-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )
            assert await handle.result() == "delivered"
            history = await handle.fetch_history()
    assert not any(e.HasField("marker_recorded_event_attributes") for e in history.events)
