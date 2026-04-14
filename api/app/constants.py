# NATS JetStream — stream and subject names (ADR-001)
# These are part of the worker contract and must not vary between environments.
# Do not move these to settings.py — they are not env-configurable by design.

NATS_STREAM_NAME = "SCRAPEFLOW"
NATS_JOBS_RUN_HTTP_SUBJECT = "scrapeflow.jobs.run.http"
NATS_JOBS_RUN_PLAYWRIGHT_SUBJECT = "scrapeflow.jobs.run.playwright"
NATS_JOBS_LLM_SUBJECT = "scrapeflow.jobs.llm"
NATS_JOBS_RESULT_SUBJECT = "scrapeflow.jobs.result"
NATS_ADVISORY_MAX_DELIVER_SUBJECT = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.SCRAPEFLOW.*"
