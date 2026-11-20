"""Records the paths compose cannot produce live (a webhook that delivers, the storage wall, retries
used up) from the mocked-activity harness into the replay corpus. Writes only missing files.

    docker compose exec api uv run python -m tests.replay.record_harness
"""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.workflows.activities.contracts import DiffOutput, DiffSummary, SendEmailOutput
from app.workflows.job import RECORD_JOB_OBJECTS_ACTIVITY, JobLLM
from app.workflows.pipeline import OUTPUTS_LEFT_ERROR, RUN_CANCELLED_ERROR, StorageUsage
from app.workflows.steps import HostSlot
from tests.replay.export_history import HISTORIES
from tests.test_destination_tests import (
    BUCKET_DELETE_REFUSED,
    BUCKET_PASSED,
    SMTP_LOGIN_FAILED,
    _secret_unreadable,
    form_input,
)
from tests.test_destination_tests import Harness as DestinationTestHarness
from tests.test_email_deliveries import Harness as EmailHarness
from tests.test_email_deliveries import _final, _transient
from tests.test_workflows_batch import Harness as BatchHarness
from tests.test_workflows_batch import _batch_input, _scrape_ok, _scraped
from tests.test_workflows_crawl import SITE, Frontier, _sitemaps, crawl_input
from tests.test_workflows_crawl import Harness as CrawlHarness
from tests.test_workflows_job import KEY
from tests.test_workflows_job import Harness as JobHarness
from tests.test_workflows_monitor_check import Harness as MonitorHarness
from tests.test_workflows_monitor_pipeline import (
    WITH_BASELINE,
    _changed,
    _context,
    _monitored,
    _same,
)
from tests.test_workflows_pipeline import (
    ALERT_ONLY,
    CLEAN_GUARD,
    CLEAN_PATH,
    R6,
    RUN_INPUT,
    SCRAPE_PATH,
    Harness,
    _met,
    _not_met,
    _stop_guard,
)
from tests.test_workflows_scheduled_job import Harness as ScheduledHarness
from tests.test_workflows_sinks import (
    BUCKET_ONLY,
    BUCKET_THEN_HOOKS,
    _flaky_bucket,
    _gone,
    _refused,
)


async def _webhook_flaky(input):
    if activity.info().attempt < 3:
        raise ApplicationError("HTTP 503", type="WebhookFailed")


async def _webhook_cancelled(input):
    raise ApplicationError("the run was cancelled", type=RUN_CANCELLED_ERROR, non_retryable=True)


async def _outputs_left(input):
    raise ApplicationError(
        "2 stored objects could not be deleted", [CLEAN_PATH, SCRAPE_PATH], type=OUTPUTS_LEFT_ERROR
    )


async def _llm_unauthorised(input):
    raise ApplicationError("401 invalid key", type="LLMFailed", non_retryable=True)


async def _llm_overloaded(input):
    raise ApplicationError("503 overloaded", type="LLMTransient")


BUSY = HostSlot(domain="example.com", wait_ms=1000)


def _changed_then_same():
    seen = []

    async def validate(input):
        seen.append(input)
        return await (_changed if len(seen) == 1 else _same)(input)

    return validate


async def _diff_gone(input):
    return DiffOutput(summary=DiffSummary(status="baseline_gone", kind=input.kind))


async def _diff_broken(input):
    raise ApplicationError("minio down", type="StorageTransient", non_retryable=True)


def _cancel_while_waiting(h):
    async def during(handle):
        await h.asked.wait()
        await handle.cancel()

    return during


def _cancel_after_three_asks(h):
    async def during(handle):
        while len(h.acquired) < 3:
            await asyncio.sleep(0.01)
        await handle.cancel()

    return during


def _cancel_in_sink_wait(h):
    async def during(handle):
        while not h.bucket_inputs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await handle.cancel()

    return during


SCENARIOS = {
    "harness_r6_webhook_delivered_after_retries": ({"webhook": _webhook_flaky}, R6),
    "harness_r6_webhook_run_cancelled_on_retry": ({"webhook": _webhook_cancelled}, R6),
    "harness_clean_validate_llm_completed": ({}, CLEAN_GUARD),
    "harness_llm_retries_exhausted": ({"llm": _llm_overloaded}, RUN_INPUT),
    "harness_storage_wall_completed": ({"usage": StorageUsage(used=150, limit=100)}, RUN_INPUT),
    # E.9 — the host-slot branch (patch "host-slot").
    "harness_r6_host_slot_waited_delivered": ({"slots": [BUSY, BUSY]}, R6),
    "harness_pipeline_host_wait_cancelled": (
        {"slots": [BUSY] * 1000, "during": _cancel_while_waiting},
        RUN_INPUT,
    ),
    # J.6 — a stop-mode Validate (patch "validate-stop").
    "harness_validate_stop_keep": ({"validate": _not_met}, _stop_guard("keep")),
    "harness_validate_stop_delete": ({"validate": _not_met}, _stop_guard("delete")),
    "harness_validate_stop_delete_gave_up": (
        {"validate": _not_met, "release_outputs": _outputs_left},
        _stop_guard("delete"),
    ),
    "harness_validate_stop_passed": ({"validate": _met}, _stop_guard("keep")),
    # L.8 — a monitor's check (patch "monitor-check").
    "harness_monitor_first_check": ({"validate": _changed}, _monitored(_context())),
    "harness_monitor_changed_page_and_extraction": (
        {"validate": _changed},
        _monitored(WITH_BASELINE),
    ),
    "harness_monitor_unchanged_stop_delete": ({"validate": _same}, _monitored(WITH_BASELINE)),
    "harness_monitor_later_gate_stop": (
        {"validate": _changed_then_same()},
        _monitored(WITH_BASELINE, later_gate=True),
    ),
    "harness_monitor_diff_baseline_gone": (
        {"validate": _changed, "diff": _diff_gone},
        _monitored(WITH_BASELINE),
    ),
    "harness_monitor_diff_unavailable": (
        {"validate": _changed, "diff": _diff_broken},
        _monitored(WITH_BASELINE),
    ),
    "harness_monitor_hand_run_no_baseline": ({"validate": _changed}, _monitored(None)),
    # J.7 — an alert-only Webhook (patch "webhook-events").
    "harness_alert_only_webhook_skipped": ({}, ALERT_ONLY),
    "harness_alert_only_webhook_run_failed": ({"llm": _llm_unauthorised}, ALERT_ONLY),
    # M.7 — sinks (patch "sinks"): bucket tries, destination webhooks, block_id + deliveries.
    "harness_sinks_bucket_retried_then_webhooks": ({"bucket": _flaky_bucket(1)}, BUCKET_THEN_HOOKS),
    "harness_sinks_bucket_cancelled_in_retry_wait": (
        {"bucket": _flaky_bucket(99), "during": _cancel_in_sink_wait},
        BUCKET_THEN_HOOKS,
    ),
    "harness_sinks_bucket_refused": ({"bucket": _refused}, BUCKET_THEN_HOOKS),
    "harness_sinks_bucket_gave_up": ({"bucket": _flaky_bucket(99)}, BUCKET_ONLY),
    "harness_sinks_destination_deleted": ({"load": _gone}, BUCKET_ONLY),
    "harness_sinks_r6_webhook_delivered": ({}, R6),
}


async def _scrape_blocked(input):
    raise ApplicationError("blocked:akamai", type="ScrapeFailed", non_retryable=True)


JOB_SCENARIOS = {
    "harness_job_completed": {},
    "harness_job_llm_completed": {"job_llm": KEY},
    "harness_job_scrape_failed": {"scrape": _scrape_blocked},
    "harness_job_llm_key_deleted": {"job_llm": JobLLM(error="LLM key not found or deleted")},
    "harness_job_storage_wall": {
        "job_llm": KEY,
        "replies": {RECORD_JOB_OBJECTS_ACTIVITY: "failed"},
    },
    # E.9 — the host-slot branch (patch "host-slot").
    "harness_job_host_slot_waited_llm_completed": {"job_llm": KEY, "slots": [BUSY, BUSY]},
    "harness_job_host_slot_failed_open": {"slots": [HostSlot()]},
    "harness_job_host_slot_scrape_failed": {"scrape": _scrape_blocked},
    "harness_job_host_wait_cancelled": {"slots": [BUSY] * 1000, "during": _cancel_while_waiting},
    "harness_job_host_wait_cancelled_after_timers": {
        "slots": [BUSY] * 1000,
        "during": _cancel_after_three_asks,
    },
}


async def _scrape_r1_blocked(input):
    if input.artifact_id == "r1":
        return await _scrape_blocked(input)
    return await _scrape_ok(input)


def _batch_cancelled_mid_scrape():
    scraping, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []

    async def scrape(input):
        seen.append(input.artifact_id)
        if len(seen) == 2:
            scraping.set()
        await release.wait()
        return _scraped(input.artifact_id)

    async def during(handle):
        await scraping.wait()
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    return {"scrape": scrape}, during


# E.1 — name -> () -> (harness kwargs, during or None), BatchInput.
BATCH_SCENARIOS = {
    "harness_batch_completed": (lambda: ({}, None), _batch_input(4, k=2)),
    "harness_batch_child_failed": (
        lambda: ({"scrape": _scrape_r1_blocked}, None),
        _batch_input(3),
    ),
    "harness_batch_cancelled_mid_scrape": (_batch_cancelled_mid_scrape, _batch_input(5, k=2)),
}


def _crawl_cancelled_mid_scrape():
    scraping, release = asyncio.Event(), asyncio.Event()
    f = Frontier(links=SITE)

    async def scrape(url):
        if url != "https://site.example/":
            scraping.set()
            await release.wait()

    async def during(handle):
        await scraping.wait()
        f.state = "cancelled"
        await handle.cancel()
        await asyncio.sleep(0.5)
        release.set()

    return f, {"scrape": scrape}, during


# E.5 — name -> () -> (Frontier, harness kwargs, during or None), CrawlInput.
CRAWL_SCENARIOS = {
    "harness_crawl_completed": (lambda: (Frontier(links=SITE), {}, None), crawl_input()),
    "harness_crawl_sitemaps": (
        lambda: (
            Frontier(links={"https://site.example/": []}),
            {"sitemaps": _sitemaps()},
            None,
        ),
        crawl_input(ignore_sitemap=False),
    ),
    "harness_crawl_storage_wall": (
        lambda: (Frontier(links=SITE, storage_full_after=2), {}, None),
        crawl_input(max_in_flight=1),
    ),
    "harness_crawl_cancelled_mid_scrape": (_crawl_cancelled_mid_scrape, crawl_input()),
    # The first run only: it ends in continue-as-new.
    "harness_crawl_continued_as_new": (
        lambda: (Frontier(links=SITE), {}, None),
        crawl_input(pages_per_run=2, max_in_flight=1),
    ),
}


# F.1 — name -> (admission verdicts in turn, what the child run does).
SCHEDULED_SCENARIOS = {
    "harness_scheduled_admitted_completed": (["admitted"], "completed"),
    "harness_scheduled_waited_then_admitted": (["waiting", "waiting", "admitted"], "completed"),
    "harness_scheduled_skipped": (["skipped"], "completed"),
    "harness_scheduled_storage_failed": (["failed"], "completed"),
    "harness_scheduled_run_failed": (["admitted"], "failed"),
}


# L.7 — MonitorCheckWorkflow's first shape (no patch): admission verdicts in turn, the child.
MONITOR_SCENARIOS = {
    "harness_monitor_check_admitted": (["admitted"], "completed"),
    "harness_monitor_check_skipped_monthly": (["skipped"], "completed"),
    "harness_monitor_check_waited_then_admitted": (["waiting", "waiting", "admitted"], "completed"),
    "harness_monitor_check_gave_up": (["waiting_unless_last"], "completed"),
    # Admission records the switched-off skip; the workflow's shape is a skip's.
    "harness_monitor_check_pipelines_disabled": (["skipped"], "completed"),
    "harness_monitor_check_failed": (["admitted"], "failed"),
}


# M.14 — WebhookDeliveryWorkflow's email branch (patch email-delivery): the sends in turn.
EMAIL_SCENARIOS = {
    "harness_email_delivered": [SendEmailOutput()],
    "harness_email_retried_then_delivered": [_transient, SendEmailOutput()],
    "harness_email_refused_final": [_final],
}


async def _test_overruns(input):
    await asyncio.sleep(3600)


# M.5 — DestinationTestWorkflow: what the delivery queue answers, and the kind tested.
DESTINATION_TEST_SCENARIOS = {
    "harness_destination_test_bucket_passed": (BUCKET_PASSED, "bucket"),
    "harness_destination_test_delete_warning": (BUCKET_DELETE_REFUSED, "bucket"),
    "harness_destination_test_smtp_login_failed": (SMTP_LOGIN_FAILED, "smtp"),
    "harness_destination_test_raised": (_secret_unreadable, "webhook"),
    "harness_destination_test_postgres_not_registered": (BUCKET_PASSED, "postgres"),
    # Real time: the test server fires start_to_close after the full 25 s.
    "harness_destination_test_timed_out": (_test_overruns, "bucket"),
}


async def main() -> None:
    runs = [(name, Harness, kwargs, run_input) for name, (kwargs, run_input) in SCENARIOS.items()]
    runs += [(name, JobHarness, kwargs, None) for name, kwargs in JOB_SCENARIOS.items()]
    for name, harness, kwargs, run_input in runs:
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        kwargs = dict(kwargs)
        during = kwargs.pop("during", None)
        h = harness(**kwargs)
        run_kwargs = {"during": during(h)} if during else {}
        await (h.run(run_input, **run_kwargs) if run_input is not None else h.run(**run_kwargs))
        path.write_text(h.history.to_json() + "\n")
        print(path)
    for name, (setup, input) in CRAWL_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        frontier, kwargs, during = setup()
        h = CrawlHarness(frontier, **kwargs)
        await h.run(input, during=during)
        history = h.first_history if name.endswith("continued_as_new") else h.history
        path.write_text(history.to_json() + "\n")
        print(path)
    for name, (verdicts, child) in SCHEDULED_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        h = ScheduledHarness(verdicts, child=child)
        await h.run()
        path.write_text(h.history.to_json() + "\n")
        print(path)
    for name, (verdicts, child) in MONITOR_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        h = MonitorHarness(verdicts, child=child)
        await h.run()
        path.write_text(h.history.to_json() + "\n")
        print(path)
    for name, sends in EMAIL_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        h = EmailHarness(sends)
        await h.run()
        path.write_text(h.history.to_json() + "\n")
        print(path)
    for name, (play, kind) in DESTINATION_TEST_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        h = DestinationTestHarness(play)
        await h.run(form_input(kind))
        path.write_text(h.history.to_json() + "\n")
        print(path)
    for name, (setup, batch_input) in BATCH_SCENARIOS.items():
        path = HISTORIES / f"{name}.json"
        if path.exists():
            continue
        kwargs, during = setup()
        h = BatchHarness(**kwargs)
        await h.run(batch_input, during=during)
        path.write_text(h.history.to_json() + "\n")
        print(path)


if __name__ == "__main__":
    asyncio.run(main())
