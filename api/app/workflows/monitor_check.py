"""One firing of a monitor's Temporal Schedule (ADR-016 §3)."""

from pydantic import BaseModel

# The Schedule starts the workflow by this type name.
MONITOR_CHECK_WORKFLOW = "MonitorCheckWorkflow"


def workflow_id(monitor_id: object) -> str:
    # The Schedule appends each firing's time to this.
    return f"monitor-check-{monitor_id}"


class MonitorCheckInput(BaseModel):
    monitor_id: str
