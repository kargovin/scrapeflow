# Temporal task-queue names (ADR-009 §8d, §9)
# These are part of the worker contract: a workflow names the queue it dispatches an activity to,
# and the worker registered for that activity listens on the same name. A mismatch raises no
# error — the task waits on a queue nobody polls until its timeout fires.
# Do not move these to settings.py — they are not env-configurable by design.

WORKFLOW_QUEUE = "workflow"
