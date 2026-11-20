from temporalio import activity

from worker import main
from worker.contracts import DELIVERY_QUEUE, TEST_WEBHOOK_ACTIVITY


def test_the_worker_serves_the_delivery_queue_eight_at_a_time():
    assert DELIVERY_QUEUE == "delivery"
    assert main.MAX_CONCURRENT_ACTIVITIES == 8
    names = {activity._Definition.must_from_callable(fn).name for fn in main.ACTIVITIES}
    assert names == {TEST_WEBHOOK_ACTIVITY}
