from temporalio import activity

from worker import main
from worker.contracts import (
    DELIVER_BUCKET_ACTIVITY,
    DELIVERY_QUEUE,
    SEND_EMAIL_ACTIVITY,
    TEST_BUCKET_ACTIVITY,
    TEST_SMTP_ACTIVITY,
    TEST_WEBHOOK_ACTIVITY,
    UNDO_BUCKET_ACTIVITY,
)


def test_the_worker_serves_the_delivery_queue_eight_at_a_time():
    assert DELIVERY_QUEUE == "delivery"
    assert main.MAX_CONCURRENT_ACTIVITIES == 8
    names = {activity._Definition.must_from_callable(fn).name for fn in main.activities(source=None)}
    assert names == {
        TEST_WEBHOOK_ACTIVITY,
        TEST_BUCKET_ACTIVITY,
        DELIVER_BUCKET_ACTIVITY,
        UNDO_BUCKET_ACTIVITY,
        TEST_SMTP_ACTIVITY,
        SEND_EMAIL_ACTIVITY,
    }
