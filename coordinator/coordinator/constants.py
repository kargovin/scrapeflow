# NATS JetStream — stream and subject names.
# These mirror api/app/constants.py and are part of the worker contract.
# Must not vary between environments — do not move to config/env vars.

NATS_STREAM_NAME = "SCRAPEFLOW"
NATS_JOBS_RUN_HTTP_SUBJECT = "scrapeflow.jobs.run.http"
NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
NATS_JOBS_RESULT_SUBJECT = "scrapeflow.jobs.result"
