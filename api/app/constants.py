# NATS JetStream — stream and subject names (ADR-001)
# These are part of the worker contract and must not vary between environments.
# Do not move these to settings.py — they are not env-configurable by design.

NATS_STREAM_NAME = "SCRAPEFLOW"
NATS_JOBS_RUN_SUBJECT = "scrapeflow.jobs.run"
NATS_JOBS_RESULT_SUBJECT = "scrapeflow.jobs.result"
