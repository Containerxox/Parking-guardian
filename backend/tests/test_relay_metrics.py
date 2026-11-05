"""Observability stage 2: relay metrics (pg_queue_messages, pg_jobs, pg_oldest_open_job_age_seconds, pg_relay_publish_*, pg_sqs_api_errors_total).

Same harness as test_relay.py (real ingest, in-memory queue on a test clock). Nothing touches AWS. The metrics are observability only:
the tests also pin that a failing metric update or queue depth poll never changes what a relay pass does, that unknown values are
NOT exported (no series, never 0), and that no label carries a company / machine / job id (cardinality).
"""
import threading
import time

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from app import adapters, metrics
from app.adapters.queue import FakeJobQueue, QueueError, SqsJobQueue
from app.extensions import db
from app.models import DetectionJob, JobStatus
from app.services import relay as relay_module
from app.services.relay import OutboxRelay, QueueDepthPoller

from .test_relay import _fail_sends, _to_dlq, relay, run_relay  # noqa: F401
from .test_worker import app_overrides, clock, drain, env, send, worker  # noqa: F401


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


@pytest.fixture(autouse=True)
def fresh_gauges():
    metrics.QUEUE_MESSAGES.clear()
    metrics.JOBS.clear()
    yield
    metrics.QUEUE_MESSAGES.clear()


# ---------------------------------------------------------------------------------------------------- queue.depths()
def test_fake_queue_depths_split_visible_and_in_flight(app, send, clock):
    send()
    send()
    with app.app_context():
        queue = adapters.queue()
        assert queue.depths() == {"main": {"visible": 2, "not_visible": 0, "delayed": 0}, "dlq": {"visible": 0, "not_visible": 0, "delayed": 0}}
        queue.receive()                                  # one message is now in flight (visibility timeout)
        assert queue.depths()["main"] == {"visible": 1, "not_visible": 1, "delayed": 0}


def test_dlq_depth_shows_a_message_the_relay_has_taken_but_not_deleted(app, send, worker, relay, clock, env):
    _to_dlq(app, send, worker, clock, env)
    with app.app_context():
        assert adapters.queue().depths()["dlq"] == {"visible": 1, "not_visible": 0, "delayed": 0}
        adapters.queue().receive_dead_letters()          # the relay read it: it is hidden while the relay works
        assert adapters.queue().depths()["dlq"] == {"visible": 0, "not_visible": 1, "delayed": 0}


def _sqs():
    client = boto3.client("sqs", region_name="ap-northeast-2", aws_access_key_id="x", aws_secret_access_key="x", config=Config(retries={"max_attempts": 0}))
    return client


QUEUE_URL = "https://sqs.ap-northeast-2.amazonaws.com/000000000000/pg-judgement.fifo"
DLQ_URL = "https://sqs.ap-northeast-2.amazonaws.com/000000000000/pg-judgement-dlq.fifo"
NAMES = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"]


def test_sqs_depths_calls_get_queue_attributes_for_both_queues():
    client = _sqs()
    with Stubber(client) as stub:
        stub.add_response("get_queue_attributes", {"Attributes": {NAMES[0]: "5", NAMES[1]: "2", NAMES[2]: "0"}}, {"QueueUrl": QUEUE_URL, "AttributeNames": NAMES})
        stub.add_response("get_queue_attributes", {"Attributes": {NAMES[0]: "0", NAMES[1]: "1", NAMES[2]: "0"}}, {"QueueUrl": DLQ_URL, "AttributeNames": NAMES})
        assert SqsJobQueue(QUEUE_URL, DLQ_URL, client=client).depths() == {
            "main": {"visible": 5, "not_visible": 2, "delayed": 0}, "dlq": {"visible": 0, "not_visible": 1, "delayed": 0}}
        stub.assert_no_pending_responses()


def test_sqs_depths_without_a_dlq_reports_only_the_main_queue():
    client = _sqs()
    with Stubber(client) as stub:
        stub.add_response("get_queue_attributes", {"Attributes": {NAMES[0]: "1", NAMES[1]: "0", NAMES[2]: "0"}}, {"QueueUrl": QUEUE_URL, "AttributeNames": NAMES})
        assert list(SqsJobQueue(QUEUE_URL, "", client=client).depths()) == ["main"]


@pytest.mark.parametrize("code,retryable", [("AccessDenied", False), ("ThrottlingException", True)])
def test_sqs_depths_failure_is_a_queue_error(code, retryable):
    client = _sqs()
    with Stubber(client) as stub:
        stub.add_client_error("get_queue_attributes", service_error_code=code, http_status_code=400)
        with pytest.raises(QueueError) as info:
            SqsJobQueue(QUEUE_URL, DLQ_URL, client=client).depths()
    assert info.value.operation == "get_queue_attributes" and info.value.code == code and info.value.retryable is retryable


# ---------------------------------------------------------------------------------------------------- poller
def test_poller_exports_all_series_and_the_success_time(app, send, clock):
    send()
    with app.app_context():
        poller = QueueDepthPoller(adapters.queue(), threading.Event())
        before = time.time()
        assert poller.poll_once() is True
    assert val("pg_queue_messages", queue="main", state="visible") == 1
    assert val("pg_queue_messages", queue="main", state="not_visible") == 0
    assert val("pg_queue_messages", queue="dlq", state="visible") == 0
    assert val("pg_queue_depth_last_success_timestamp_seconds") >= before - 1


def test_failed_poll_removes_the_series_instead_of_showing_zero_and_counts_the_error(app, send, clock, caplog):
    send()
    with app.app_context():
        queue = adapters.queue()
        poller = QueueDepthPoller(queue, threading.Event())
        assert poller.poll_once() is True
        errors_before = val("pg_sqs_api_errors_total", operation="get_queue_attributes") or 0
        queue.fail_next("get_queue_attributes", "AccessDenied")
        assert poller.poll_once() is False
        assert poller.poll_once() is True                    # recovers by itself
        queue.fail_next("get_queue_attributes", "AccessDenied")
        poller.poll_once()
    assert val("pg_queue_messages", queue="main", state="visible") is None      # unknown, not 0 and not the stale value
    assert val("pg_sqs_api_errors_total", operation="get_queue_attributes") == errors_before + 2
    assert caplog.text.count("queue depth poll failed") == 2                    # logged once per outage (two outages), not per poll


def test_poller_thread_runs_beside_the_loop_and_stops_with_the_event(app, send, clock):
    send()
    stop = threading.Event()
    with app.app_context():
        poller = QueueDepthPoller(adapters.queue(), stop, interval=0.05)
        poller.start()
        for _ in range(100):
            if val("pg_queue_messages", queue="main", state="visible") == 1:
                break
            time.sleep(0.02)
        stop.set()
        poller.join(timeout=2)
    assert not poller.is_alive() and val("pg_queue_messages", queue="main", state="visible") == 1


# ---------------------------------------------------------------------------------------------------- database-based gauges
def _oldest_open():
    return val("pg_oldest_open_job_age_seconds")


def test_oldest_open_job_age_counts_pending_queued_and_processing_but_not_done_or_dead(app, send, relay, worker, clock, env):
    run_relay(app, relay)
    assert _oldest_open() == 0                                                  # nothing open
    restore = _fail_sends(app, 1)
    send()                                                                       # pending_publish (ingest could not send)
    restore()
    clock.advance(seconds=30)
    run_relay(app, relay)                                                        # published in this pass -> queued, still unfinished
    assert 29 <= _oldest_open() <= 40
    clock.advance(seconds=30)
    run_relay(app, relay)
    assert 59 <= _oldest_open() <= 70                                            # queued jobs keep ageing until judged
    drain(app, worker)
    run_relay(app, relay)
    assert _oldest_open() == 0                                                   # done = finished
    job_id = send()
    env.fail_times = 3
    for _ in range(3):
        drain(app, worker)
        clock.advance(seconds=61)
    with app.app_context():
        adapters.queue().receive()
    run_relay(app, relay, "sweep_dead_letters")
    clock.advance(seconds=100)
    stats = run_relay(app, relay)
    with app.app_context():
        assert db.session.get(DetectionJob, __import__("uuid").UUID(job_id)).status == JobStatus.DEAD_LETTERED
    assert stats["oldest_open_job_age_seconds"] == 0 and _oldest_open() == 0   # dead_lettered is not "open": it is counted separately


def test_pending_publish_age_gauge_follows_the_oldest_pending_job(app, send, relay, clock):
    restore = _fail_sends(app, 100)
    send()
    clock.advance(seconds=90)
    run_relay(app, relay)
    restore()
    assert 89 <= val("pg_oldest_pending_publish_age_seconds") <= 100


def test_job_counts_by_status_include_dead_lettered_and_are_throttled(app, send, worker, relay, clock, env, monkeypatch):
    restore = _fail_sends(app, 100)
    send()
    restore()
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    with app.app_context():
        r = OutboxRelay(clock=clock)
        r.run_once()
        assert val("pg_jobs", status="pending_publish") == 1 and val("pg_jobs", status="dead_lettered") == 0
        _fail_sends(app, 0)
        send()                                                                    # a second job appears...
        r.run_once()                                                              # ...but counts are refreshed at most every 15 s
        assert val("pg_jobs", status="pending_publish") + val("pg_jobs", status="queued") == 1
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0 + relay_module.JOB_COUNT_INTERVAL_SECONDS + 1)
        r.run_once()
        assert val("pg_jobs", status="pending_publish") + val("pg_jobs", status="queued") == 2


def test_dead_lettered_jobs_stay_visible_in_pg_jobs_after_the_dlq_is_drained(app, send, worker, relay, clock, env):
    _to_dlq(app, send, worker, clock, env)
    run_relay(app, relay)                                                         # sweeps the DLQ and records the gauges
    with app.app_context():
        assert adapters.queue().depths()["dlq"] == {"visible": 0, "not_visible": 0, "delayed": 0}    # the SQS DLQ is empty again...
        r = OutboxRelay(clock=clock)
        r._last_job_counts = float("-inf")
        r.run_once()
    assert val("pg_jobs", status="dead_lettered") == 1                           # ...but the durable signal remains until a redrive


# ---------------------------------------------------------------------------------------------------- publish metrics
def test_publish_success_failure_and_delay_are_recorded(app, send, relay, clock):
    ok_before = val("pg_relay_publish_total", outcome="success") or 0
    bad_before = val("pg_relay_publish_total", outcome="failure") or 0
    err_before = val("pg_sqs_api_errors_total", operation="send") or 0
    delay_before = val("pg_relay_publish_delay_seconds_sum") or 0
    restore = _fail_sends(app, 2)                                                 # ingest's send fails, then the relay's first retry fails too
    send()
    clock.advance(seconds=2)
    assert run_relay(app, relay, "publish_pending") == 0
    clock.advance(seconds=10)
    assert run_relay(app, relay, "publish_pending") == 1
    restore()
    assert val("pg_relay_publish_total", outcome="failure") == bad_before + 1
    assert val("pg_sqs_api_errors_total", operation="send") == err_before + 1
    assert val("pg_relay_publish_total", outcome="success") == ok_before + 1
    assert val("pg_relay_publish_delay_seconds_sum") - delay_before >= 12        # created 12 s (test clock) before the relay's send
    assert val("pg_relay_publish_delay_seconds_count") >= 1


def test_a_dlq_read_failure_is_counted(app, relay, clock):
    with app.app_context():
        adapters.queue().receive_dead_letters = lambda max_messages=10: (_ for _ in ()).throw(QueueError("receive_dead_letters", "AccessDenied"))
    before = val("pg_sqs_api_errors_total", operation="receive_dead_letters") or 0
    run_relay(app, relay, "sweep_dead_letters")
    assert val("pg_sqs_api_errors_total", operation="receive_dead_letters") == before + 1


# ---------------------------------------------------------------------------------------------------- never changes behaviour
def test_a_failing_metric_update_does_not_break_the_relay_pass(app, send, relay, clock, monkeypatch):
    restore = _fail_sends(app, 1)
    send()
    restore()
    clock.advance(seconds=2)

    class Broken:
        def labels(self, *a):
            raise RuntimeError("metrics are broken")

    monkeypatch.setattr(metrics, "JOBS", Broken())
    stats = run_relay(app, relay)
    assert stats["published"] == 1 and "oldest_open_job_age_seconds" in stats       # the pass did its work
    with app.app_context():
        assert db.session.scalars(db.select(DetectionJob)).one().status == JobStatus.QUEUED


# ---------------------------------------------------------------------------------------------------- exposition format and labels
ALLOWED = {"queue": {"main", "dlq"}, "state": {"visible", "not_visible", "delayed"}, "status": {"pending_publish", "queued", "processing", "dead_lettered"},
           "outcome": {"success", "failure"}, "operation": {"send", "receive_dead_letters", "get_queue_attributes"}}
NEW = ("pg_queue_messages", "pg_queue_depth_last_success_timestamp_seconds", "pg_sqs_api_errors", "pg_jobs", "pg_oldest_open_job_age_seconds",
       "pg_oldest_pending_publish_age_seconds", "pg_relay_publish", "pg_relay_publish_delay_seconds", "pg_relay_last_pass_timestamp_seconds")


def test_exposition_format_parses_and_labels_are_small_fixed_sets(app, send, worker, relay, clock, env):
    send()
    with app.app_context():
        QueueDepthPoller(adapters.queue(), threading.Event()).poll_once()
        r = OutboxRelay(clock=clock)
        r._last_job_counts = float("-inf")
        r.run_once()
    families = {f.name: f for f in text_string_to_metric_families(generate_latest(REGISTRY).decode("utf-8"))}
    for name in NEW:
        assert name in families, name
    types = {f.name: f.type for f in families.values() if f.name in NEW}
    assert types["pg_queue_messages"] == "gauge" and types["pg_jobs"] == "gauge" and types["pg_relay_publish"] == "counter"
    assert types["pg_sqs_api_errors"] == "counter" and types["pg_relay_publish_delay_seconds"] == "histogram"
    for name in NEW:
        for sample in families[name].samples:
            for label, value in sample.labels.items():
                if label == "le":
                    continue
                assert label in ALLOWED, (name, label)                                # no company_id / machine_id / job_id label can exist
                assert value in ALLOWED[label], (name, label, value)


# ---------------------------------------------------------------------------------------------------- CLI
def test_once_does_not_start_the_metrics_server_but_the_loop_does(app, monkeypatch):
    calls = {"server": 0, "poller": 0}
    monkeypatch.setattr("app.metrics.start_metrics_server", lambda port: calls.__setitem__("server", calls["server"] + 1))
    monkeypatch.setattr(QueueDepthPoller, "start", lambda self: calls.__setitem__("poller", calls["poller"] + 1))
    monkeypatch.setattr(OutboxRelay, "run_loop", lambda self, interval=2.0, clean_every=1800: 0)
    runner = app.test_cli_runner()
    assert runner.invoke(args=["outbox-relay", "--once"]).exit_code == 0
    assert calls == {"server": 0, "poller": 0}                                       # one-shot passes expose nothing
    assert runner.invoke(args=["outbox-relay"]).exit_code == 0
    assert calls == {"server": 1, "poller": 1}
