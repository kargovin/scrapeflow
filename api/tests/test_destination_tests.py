"""M.5 — the destination test (ADR-015 §2d): DestinationTestWorkflow on the workflow queue runs the
kind's test on the delivery queue; the API waits ≤ 30 s from three call sites — an unsaved form,
save and edit (refused on any failed check, saved on a warning), and a saved destination."""

import asyncio
import json
import socket
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.core import destination_tests
from app.core.db import AsyncSessionLocal
from app.core.destinations import decrypt_secret, encrypt_secret
from app.main import app
from app.models.destination import Destination
from app.workflows.activities.contracts import (
    TEST_BUCKET_ACTIVITY,
    TEST_SMTP_ACTIVITY,
    TEST_WEBHOOK_ACTIVITY,
    CheckResult,
    DestinationTestInput,
    DestinationTestOutput,
)
from app.workflows.client import get_temporal_or_none
from app.workflows.destination_test import (
    EXECUTION_TIMEOUT,
    TEST_QUEUE_WAIT,
    TEST_TIMEOUT,
    TEST_UNAVAILABLE_ERROR,
    DestinationTestWorkflow,
    failed_test,
)
from app.workflows.queues import DELIVERY_QUEUE, WORKFLOW_QUEUE
from tests.test_destinations import BODIES, SENTINEL, _row

DELETE_REFUSED = (
    "delete refused — rollback to this bucket will fail; left `scrapeflow/.scrapeflow-test-1` "
    "(AccessDenied)"
)


def _passed(*names: str) -> list[CheckResult]:
    return [CheckResult(check=n, outcome="passed") for n in names]


BUCKET_PASSED = DestinationTestOutput(checks=_passed("address", "tls", "write", "delete"))
BUCKET_DELETE_REFUSED = DestinationTestOutput(
    checks=[
        *_passed("address", "tls", "write"),
        CheckResult(check="delete", outcome="warning", message=DELETE_REFUSED),
    ]
)
SMTP_LOGIN_FAILED = DestinationTestOutput(
    checks=[
        *_passed("address", "tls"),
        CheckResult(check="login", outcome="failed", message="login refused (535)"),
    ]
)


def form_input(kind: str) -> DestinationTestInput:
    body = BODIES[kind]
    return DestinationTestInput(
        kind=kind,
        address=body.get("address", {}),
        encrypted_secret=encrypt_secret(body["secret"]),
    )


# --- the workflow ---------------------------------------------------------------------------


async def _secret_unreadable(input):
    raise ApplicationError(
        "the destination's secret cannot be read", type="DestinationSecret", non_retryable=True
    )


class Harness:
    """The workflow with stand-in tests on the delivery queue. ``play`` answers every test (a
    callable raises); TestPostgres is not registered, as on today's delivery worker."""

    def __init__(self, play):
        self.play = play
        self.seen: list[tuple[str, DestinationTestInput]] = []

    async def run(self, input: DestinationTestInput) -> DestinationTestOutput:
        async def answer(name: str, inp: DestinationTestInput) -> DestinationTestOutput:
            self.seen.append((name, inp))
            if callable(self.play):
                return await self.play(inp)
            return self.play

        @activity.defn(name=TEST_BUCKET_ACTIVITY)
        async def bucket(inp: DestinationTestInput) -> DestinationTestOutput:
            return await answer(TEST_BUCKET_ACTIVITY, inp)

        @activity.defn(name=TEST_SMTP_ACTIVITY)
        async def smtp(inp: DestinationTestInput) -> DestinationTestOutput:
            return await answer(TEST_SMTP_ACTIVITY, inp)

        @activity.defn(name=TEST_WEBHOOK_ACTIVITY)
        async def webhook(inp: DestinationTestInput) -> DestinationTestOutput:
            return await answer(TEST_WEBHOOK_ACTIVITY, inp)

        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env:
            async with (
                Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DestinationTestWorkflow]),
                Worker(env.client, task_queue=DELIVERY_QUEUE, activities=[bucket, smtp, webhook]),
            ):
                handle = await env.client.start_workflow(
                    DestinationTestWorkflow.run,
                    input,
                    id=f"destination-test-{uuid.uuid4()}",
                    task_queue=WORKFLOW_QUEUE,
                )
                result = await handle.result()
                self.history = await handle.fetch_history()
                return result


@pytest.mark.parametrize(
    "kind, name",
    [
        ("bucket", TEST_BUCKET_ACTIVITY),
        ("smtp", TEST_SMTP_ACTIVITY),
        ("webhook", TEST_WEBHOOK_ACTIVITY),
    ],
)
async def test_each_kind_runs_its_test_once_on_the_delivery_queue(kind, name):
    h = Harness(BUCKET_DELETE_REFUSED)
    inp = form_input(kind)
    assert await h.run(inp) == BUCKET_DELETE_REFUSED
    assert h.seen == [(name, inp)]

    [scheduled] = [
        e.activity_task_scheduled_event_attributes
        for e in h.history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled.activity_type.name == name
    assert scheduled.task_queue.name == DELIVERY_QUEUE
    assert scheduled.retry_policy.maximum_attempts == 1
    assert scheduled.schedule_to_start_timeout.seconds == TEST_QUEUE_WAIT.total_seconds()
    assert scheduled.start_to_close_timeout.seconds == TEST_TIMEOUT.total_seconds()
    assert (TEST_QUEUE_WAIT + TEST_TIMEOUT).total_seconds() < destination_tests.API_WAIT_SECONDS


async def test_the_history_holds_the_secret_as_ciphertext_only():
    h = Harness(SMTP_LOGIN_FAILED)
    assert await h.run(form_input("smtp")) == SMTP_LOGIN_FAILED
    assert SENTINEL not in h.history.to_json()


async def test_a_test_that_raises_is_one_failed_check_with_the_workers_message():
    out = await Harness(_secret_unreadable).run(form_input("webhook"))
    assert out.checks == [
        CheckResult(
            check="test", outcome="failed", message="the destination's secret cannot be read"
        )
    ]


async def test_a_kind_no_delivery_worker_tests_is_a_failed_check():
    """Postgres before O.1: the delivery worker answers that it has no such activity."""
    h = Harness(BUCKET_PASSED)
    out = await h.run(form_input("postgres"))
    assert h.seen == []
    assert out.checks == [
        CheckResult(
            check="test",
            outcome="failed",
            message="the delivery worker cannot test a postgres destination",
        )
    ]


def _timeout(kind: TimeoutType) -> ActivityTimeoutError:
    return ActivityTimeoutError("timed out", type=kind, last_heartbeat_details=[])


def test_a_test_that_overruns_is_a_failed_check():
    assert failed_test("bucket", _timeout(TimeoutType.START_TO_CLOSE)) == CheckResult(
        check="test", outcome="failed", message="the test did not finish in time"
    )
    assert failed_test("smtp", RuntimeError("x")).message == "the test failed (RuntimeError)"


def test_a_test_no_delivery_worker_took_fails_the_workflow():
    # The test server never expires schedule_to_start: the mapping is checked here, live on compose.
    with pytest.raises(ApplicationError) as exc:
        failed_test("bucket", _timeout(TimeoutType.SCHEDULE_TO_START))
    assert (exc.value.type, exc.value.non_retryable) == (TEST_UNAVAILABLE_ERROR, True)
    assert exc.value.message == "no delivery worker took the test; try again"


# --- the API's wait -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def headers(db_api_key):
    raw_key, _ = db_api_key
    return {"X-API-Key": raw_key}


@pytest.fixture(autouse=True)
def public_dns():
    with patch("app.core.security.socket") as fake_socket:
        fake_socket.gaierror = socket.gaierror
        fake_socket.getaddrinfo.return_value = [(None, None, None, None, ("93.184.216.34", 0))]
        yield fake_socket


def _answers(mock_temporal, *outputs) -> list[DestinationTestInput]:
    """The fake engine answers each test in turn; returns the inputs it was handed."""
    seen: list[DestinationTestInput] = []
    plays = list(outputs)

    async def execute(run, inp, **kwargs):
        assert run == DestinationTestWorkflow.run
        assert kwargs["task_queue"] == WORKFLOW_QUEUE
        assert kwargs["id"].startswith("destination-test-")
        assert kwargs["execution_timeout"] == EXECUTION_TIMEOUT
        seen.append(inp)
        play = plays.pop(0)
        if isinstance(play, BaseException):
            raise play
        return play

    mock_temporal.execute_workflow.side_effect = execute
    return seen


async def test_an_unsaved_form_reports_each_check_and_stores_nothing(
    client, headers, mock_temporal, db_user
):
    seen = _answers(mock_temporal, BUCKET_DELETE_REFUSED)
    resp = await client.post(
        "/destinations/test", json={"name": "draft", **BODIES["bucket"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "passed": True,
        "checks": [
            {"check": "address", "outcome": "passed", "message": None},
            {"check": "tls", "outcome": "passed", "message": None},
            {"check": "write", "outcome": "passed", "message": None},
            {"check": "delete", "outcome": "warning", "message": DELETE_REFUSED},
        ],
    }
    [inp] = seen
    assert inp.kind == "bucket"
    assert inp.address["prefix"] == "scrapeflow"
    assert decrypt_secret(inp.encrypted_secret) == BODIES["bucket"]["secret"]
    assert SENTINEL not in inp.model_dump_json()
    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            select(func.count()).select_from(Destination).where(Destination.user_id == db_user.id)
        )
    assert count == 0


async def test_a_form_without_a_name_is_tested(client, headers, mock_temporal):
    _answers(mock_temporal, SMTP_LOGIN_FAILED)
    resp = await client.post("/destinations/test", json=BODIES["smtp"], headers=headers)
    assert resp.status_code == 200
    assert resp.json()["passed"] is False
    assert resp.json()["checks"][-1]["message"] == "login refused (535)"


async def test_a_private_address_is_refused_before_any_test(
    client, headers, mock_temporal, public_dns
):
    public_dns.getaddrinfo.return_value = [(None, None, None, None, ("10.0.0.5", 0))]
    resp = await client.post("/destinations/test", json=BODIES["webhook"], headers=headers)
    assert resp.status_code == 400
    mock_temporal.execute_workflow.assert_not_called()


async def test_a_destination_that_fails_its_test_is_not_saved(client, headers, mock_temporal):
    _answers(mock_temporal, SMTP_LOGIN_FAILED)
    resp = await client.post(
        "/destinations", json={"name": "mail", **BODIES["smtp"]}, headers=headers
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "destination_test_failed"
    assert detail["message"] == "The destination test failed — login: login refused (535)"
    assert [c["check"] for c in detail["checks"]] == ["address", "tls", "login"]
    assert (await client.get("/destinations", headers=headers)).json() == []


async def test_a_warning_saves_and_comes_back_with_the_save(client, headers, mock_temporal):
    _answers(mock_temporal, BUCKET_DELETE_REFUSED)
    resp = await client.post(
        "/destinations", json={"name": "lake", **BODIES["bucket"]}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["test"]["passed"] is True
    assert resp.json()["test"]["checks"][-1]["outcome"] == "warning"
    row = await _row(resp.json()["id"])
    assert row is not None and row.name == "lake"
    # Never stored (§2a): a later read has no test.
    assert "test" not in (await client.get(f"/destinations/{row.id}", headers=headers)).json()


async def test_an_edit_that_fails_its_test_changes_nothing(client, headers, mock_temporal):
    _answers(mock_temporal, BUCKET_PASSED, BUCKET_DELETE_REFUSED, SMTP_LOGIN_FAILED)
    created = (
        await client.post("/destinations", json={"name": "mail", **BODIES["smtp"]}, headers=headers)
    ).json()
    before = await _row(created["id"])

    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"name": "renamed", "secret": {"password": "typo"}},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["test"]["checks"][-1]["outcome"] == "warning"

    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"name": "renamed-again", "address": {"host": "mail2.example.com"}},
        headers=headers,
    )
    assert resp.status_code == 422
    after = await _row(created["id"])
    assert after.name == "renamed"
    assert after.address["host"] == before.address["host"]
    assert decrypt_secret(after.encrypted_secret)["password"] == "typo"


async def test_an_edit_tests_the_merged_form(client, headers, mock_temporal):
    seen = _answers(mock_temporal, BUCKET_PASSED, BUCKET_PASSED)
    created = (
        await client.post(
            "/destinations", json={"name": "lake", **BODIES["bucket"]}, headers=headers
        )
    ).json()
    resp = await client.patch(
        f"/destinations/{created['id']}",
        json={"address": {"bucket": "other-results"}},
        headers=headers,
    )
    assert resp.status_code == 200
    edited = seen[1]
    assert edited.address == {**created["address"], "bucket": "other-results"}
    assert decrypt_secret(edited.encrypted_secret) == BODIES["bucket"]["secret"]


async def test_a_rename_runs_no_test_and_needs_no_engine(client, headers, mock_temporal):
    created = (
        await client.post("/destinations", json={"name": "a", **BODIES["webhook"]}, headers=headers)
    ).json()
    mock_temporal.execute_workflow.reset_mock()
    app.dependency_overrides[get_temporal_or_none] = lambda: None
    try:
        resp = await client.patch(
            f"/destinations/{created['id']}", json={"name": "b"}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["test"] is None
        assert resp.json()["name"] == "b"
        resp = await client.patch(
            f"/destinations/{created['id']}",
            json={"secret": {"secret": "new"}},
            headers=headers,
        )
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.pop(get_temporal_or_none, None)
    mock_temporal.execute_workflow.assert_not_called()


async def test_a_saved_destination_is_tested_as_stored(client, headers, mock_temporal, other_user):
    seen = _answers(mock_temporal, BUCKET_PASSED, SMTP_LOGIN_FAILED)
    created = (
        await client.post("/destinations", json={"name": "mail", **BODIES["smtp"]}, headers=headers)
    ).json()
    row = await _row(created["id"])

    resp = await client.post(f"/destinations/{created['id']}/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["passed"] is False
    assert seen[1] == DestinationTestInput(
        kind="smtp", address=row.address, encrypted_secret=row.encrypted_secret
    )
    # Nothing stored: the failed test leaves the row as it was.
    assert (await _row(created["id"])).updated_at == row.updated_at

    async with AsyncSessionLocal() as db:
        theirs = Destination(
            user_id=other_user.id,
            name="theirs",
            kind="webhook",
            encrypted_secret=encrypt_secret({"url": "https://x.example.com/"}),
            display_host="x.example.com",
        )
        db.add(theirs)
        await db.commit()
    resp = await client.post(f"/destinations/{theirs.id}/test", headers=headers)
    assert resp.status_code == 404
    assert len(seen) == 2


async def test_an_unsaved_edit_is_tested_over_the_stored_form_and_stores_nothing(
    client, headers, mock_temporal, other_user, public_dns
):
    seen = _answers(mock_temporal, BUCKET_PASSED, BUCKET_DELETE_REFUSED)
    created = (
        await client.post(
            "/destinations", json={"name": "lake", **BODIES["bucket"]}, headers=headers
        )
    ).json()
    row = await _row(created["id"])
    url = f"/destinations/{created['id']}/test"

    resp = await client.post(
        url,
        json={"address": {"bucket": "other-results"}, "secret": {"secret_key": "new"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"][-1]["outcome"] == "warning"
    tested = seen[1]
    assert tested.address == {**created["address"], "bucket": "other-results"}
    assert decrypt_secret(tested.encrypted_secret) == {
        **BODIES["bucket"]["secret"],
        "secret_key": "new",
    }
    after = await _row(created["id"])
    assert (after.address, after.encrypted_secret, after.updated_at) == (
        row.address,
        row.encrypted_secret,
        row.updated_at,
    )

    # The PATCH's checks: a field the kind does not have, a name, a private host.
    resp = await client.post(url, json={"address": {"nope": 1}}, headers=headers)
    assert resp.status_code == 422
    resp = await client.post(url, json={"name": "x"}, headers=headers)
    assert resp.status_code == 422
    public_dns.getaddrinfo.return_value = [(None, None, None, None, ("10.0.0.5", 0))]
    resp = await client.post(
        url, json={"address": {"endpoint": "https://lan.example.com"}}, headers=headers
    )
    assert resp.status_code == 400
    assert len(seen) == 2


@pytest.mark.parametrize(
    "failure, message",
    [
        (
            WorkflowFailureError(
                cause=ApplicationError(
                    "no delivery worker took the test; try again", type=TEST_UNAVAILABLE_ERROR
                )
            ),
            "no delivery worker took the test; try again",
        ),
        (
            WorkflowFailureError(cause=ApplicationError("boom", type="Other")),
            "The destination test could not run",
        ),
        (RuntimeError("engine down"), "Workflow engine unavailable"),
    ],
)
async def test_a_test_that_could_not_run_is_a_503_and_saves_nothing(
    client, headers, mock_temporal, failure, message
):
    _answers(mock_temporal, failure, failure)
    resp = await client.post("/destinations/test", json=BODIES["webhook"], headers=headers)
    assert (resp.status_code, resp.json()["detail"]) == (503, message)
    resp = await client.post(
        "/destinations", json={"name": "hook", **BODIES["webhook"]}, headers=headers
    )
    assert (resp.status_code, resp.json()["detail"]) == (503, message)
    assert (await client.get("/destinations", headers=headers)).json() == []


async def test_the_api_stops_waiting_at_its_limit(client, headers, mock_temporal, monkeypatch):
    monkeypatch.setattr(destination_tests, "API_WAIT_SECONDS", 0.05)

    async def stuck(*args, **kwargs):
        await asyncio.sleep(5)

    mock_temporal.execute_workflow.side_effect = stuck
    resp = await client.post("/destinations/test", json=BODIES["webhook"], headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "The destination test did not finish in 0.05 s"


async def test_a_test_with_no_check_is_not_a_pass(client, headers, mock_temporal):
    _answers(mock_temporal, DestinationTestOutput(checks=[]))
    resp = await client.post(
        "/destinations", json={"name": "hook", **BODIES["webhook"]}, headers=headers
    )
    assert resp.status_code == 422
    assert json.loads(resp.text)["detail"]["message"].endswith("the test ran no check")
