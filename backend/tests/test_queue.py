"""Judgement queue adapter (ADR 0019 decisions 2, 3, 7; design 6.3, 6.7, 6.8, 6.11).

Two implementations of one contract:
- SqsJobQueue: SQS FIFO through boto3. Tested with botocore's Stubber, which checks every call against the
  real SQS API model and never opens a network connection.
- FakeJobQueue: in-memory FIFO with the SQS behaviour later steps rely on (dedup window, one in-flight message
  per group, visibility timeout, receive count, redrive to a dead-letter queue). For local runs and tests.
Nothing here touches AWS.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import ANY, Stubber

from app.adapters.queue import (FakeJobQueue, QueueError, QueueMessage, SqsJobQueue, build_queue, group_id_for,
                                message_body)

QUEUE_URL = "https://sqs.ap-northeast-2.amazonaws.com/000000000000/pg-judgement.fifo"
DLQ_URL = "https://sqs.ap-northeast-2.amazonaws.com/000000000000/pg-judgement-dlq.fifo"
JOB = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f")


# --- shared message format -------------------------------------------------------------------------

def test_group_is_the_machine_and_the_body_carries_only_the_job_id():
    """Design 6.3: MessageGroupId = "m-<machine_id>", no company or lot ids; the job row holds the rest."""
    assert group_id_for(7) == "m-7"
    assert json.loads(message_body(JOB)) == {"job_id": str(JOB)}


# --- SqsJobQueue with a stubbed client ---------------------------------------------------------------

def _client():
    return boto3.client("sqs", region_name="ap-northeast-2", aws_access_key_id="test",
                        aws_secret_access_key="test", config=Config(retries={"max_attempts": 0}))


@pytest.fixture
def sqs():
    client = _client()
    with Stubber(client) as stub:
        yield SqsJobQueue(QUEUE_URL, DLQ_URL, client=client), stub
        stub.assert_no_pending_responses()


def test_send_uses_fifo_group_and_job_id_as_deduplication_id(sqs):
    queue, stub = sqs
    stub.add_response("send_message", {"MessageId": "m-1", "SequenceNumber": "1"}, {
        "QueueUrl": QUEUE_URL, "MessageBody": message_body(JOB),
        "MessageGroupId": "m-7", "MessageDeduplicationId": str(JOB)})
    assert queue.send(JOB, machine_id=7) == "m-1"


@pytest.mark.parametrize("code,retryable", [
    ("ThrottlingException", True),
    ("RequestThrottled", True),
    ("ServiceUnavailable", True),
    ("InternalError", True),
    ("AccessDenied", False),
    ("AWS.SimpleQueueService.NonExistentQueue", False),
    ("InvalidParameterValue", False),
])
def test_send_failure_raises_queue_error_with_retry_hint(sqs, code, retryable):
    """The adapter does not hide failures: the caller leaves the job pending_publish and the relay retries
    (design 6.7). `retryable` only tells whether waiting can help (for logs and alerts)."""
    queue, stub = sqs
    stub.add_client_error("send_message", service_error_code=code, http_status_code=400)
    with pytest.raises(QueueError) as info:
        queue.send(JOB, machine_id=7)
    assert info.value.operation == "send" and info.value.code == code and info.value.retryable is retryable


def test_network_failure_is_a_retryable_queue_error():
    class Unreachable:
        def send_message(self, **kwargs):
            raise EndpointConnectionError(endpoint_url=QUEUE_URL)

    with pytest.raises(QueueError) as info:
        SqsJobQueue(QUEUE_URL, DLQ_URL, client=Unreachable()).send(JOB, machine_id=7)
    assert info.value.retryable is True and info.value.code == "EndpointConnectionError"


def test_receive_long_polls_one_message_and_reports_the_receive_count(sqs):
    queue, stub = sqs
    stub.add_response("receive_message", {"Messages": [{
        "MessageId": "m-1", "ReceiptHandle": "rh-1", "Body": message_body(JOB),
        "Attributes": {"ApproximateReceiveCount": "2", "MessageGroupId": "m-7"}}]}, {
        "QueueUrl": QUEUE_URL, "MaxNumberOfMessages": 1, "WaitTimeSeconds": 20,
        "MessageSystemAttributeNames": ["ApproximateReceiveCount", "MessageGroupId"]})
    assert queue.receive() == [QueueMessage(job_id=JOB, receipt_handle="rh-1", receive_count=2, message_id="m-1",
                                            group_id="m-7", body=message_body(JOB))]


def test_receive_with_nothing_waiting_returns_an_empty_list(sqs):
    queue, stub = sqs
    stub.add_response("receive_message", {}, {
        "QueueUrl": QUEUE_URL, "MaxNumberOfMessages": 1, "WaitTimeSeconds": 20,
        "MessageSystemAttributeNames": ANY})
    assert queue.receive() == []


def test_unreadable_body_is_returned_without_a_job_id_not_raised(sqs):
    """A malformed message must not crash the worker loop; the worker decides (log + delete) in step 5."""
    queue, stub = sqs
    stub.add_response("receive_message", {"Messages": [{
        "MessageId": "m-9", "ReceiptHandle": "rh-9", "Body": "not json",
        "Attributes": {"ApproximateReceiveCount": "1", "MessageGroupId": "m-7"}}]}, {
        "QueueUrl": QUEUE_URL, "MaxNumberOfMessages": 1, "WaitTimeSeconds": 20,
        "MessageSystemAttributeNames": ANY})
    [message] = queue.receive()
    assert message.job_id is None and message.body == "not json" and message.receipt_handle == "rh-9"


def test_receive_failure_raises_queue_error(sqs):
    queue, stub = sqs
    stub.add_client_error("receive_message", service_error_code="ServiceUnavailable", http_status_code=503)
    with pytest.raises(QueueError) as info:
        queue.receive()
    assert info.value.operation == "receive" and info.value.retryable is True


def test_delete_and_visibility_extension(sqs):
    queue, stub = sqs
    stub.add_response("change_message_visibility", {}, {
        "QueueUrl": QUEUE_URL, "ReceiptHandle": "rh-1", "VisibilityTimeout": 120})
    stub.add_response("delete_message", {}, {"QueueUrl": QUEUE_URL, "ReceiptHandle": "rh-1"})
    queue.extend_visibility("rh-1", 120)
    queue.delete("rh-1")


def test_delete_with_an_expired_receipt_is_a_non_retryable_error(sqs):
    """Design 6.11: if DeleteMessage fails the message comes back after the visibility timeout and the claim
    sees the job is done. The adapter only reports it; retrying the same receipt cannot succeed."""
    queue, stub = sqs
    stub.add_client_error("delete_message", service_error_code="ReceiptHandleIsInvalid", http_status_code=400)
    with pytest.raises(QueueError) as info:
        queue.delete("rh-old")
    assert info.value.operation == "delete" and info.value.retryable is False


def test_dead_letters_are_read_from_and_deleted_on_the_dlq(sqs):
    queue, stub = sqs
    stub.add_response("receive_message", {"Messages": [{
        "MessageId": "d-1", "ReceiptHandle": "drh-1", "Body": message_body(JOB),
        "Attributes": {"ApproximateReceiveCount": "1", "MessageGroupId": "m-7"}}]}, {
        "QueueUrl": DLQ_URL, "MaxNumberOfMessages": 10, "WaitTimeSeconds": 0,
        "MessageSystemAttributeNames": ANY})
    stub.add_response("delete_message", {}, {"QueueUrl": DLQ_URL, "ReceiptHandle": "drh-1"})
    [message] = queue.receive_dead_letters()
    assert message.job_id == JOB
    queue.delete_dead_letter("drh-1")


def test_sqs_queue_requires_a_fifo_queue_url():
    with pytest.raises(ValueError):
        SqsJobQueue("", DLQ_URL, client=_client())
    with pytest.raises(ValueError):
        SqsJobQueue(QUEUE_URL.replace(".fifo", ""), DLQ_URL, client=_client())


def test_default_client_has_bounded_retries_and_a_read_timeout_above_the_long_poll():
    """boto3 retries transient errors inside one call (bounded); the relay's own backoff handles the rest.
    The read timeout must exceed the 20 s long poll or every empty receive would time out."""
    queue = SqsJobQueue(QUEUE_URL, DLQ_URL, region="ap-northeast-2")
    config = queue.client_config()
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    assert config.read_timeout > 20 and config.connect_timeout <= 5


# --- FakeJobQueue: the same contract plus the SQS behaviour later steps rely on ---------------------

class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(clock):
    return FakeJobQueue(clock=clock, visibility_timeout=60, max_receive_count=3)


def _job():
    return uuid.uuid4()


def test_fake_send_then_receive_then_delete(fake):
    job = _job()
    fake.send(job, machine_id=1)
    [message] = fake.receive()
    assert message.job_id == job and message.group_id == "m-1" and message.receive_count == 1
    fake.delete(message.receipt_handle)
    assert fake.receive() == [] and fake.depth() == 0


def test_fake_drops_a_duplicate_send_within_five_minutes(fake, clock):
    """Design 6.3: the same job sent twice by the relay inside the dedup window becomes one message."""
    job = _job()
    first = fake.send(job, machine_id=1)
    clock.advance(minutes=4, seconds=59)
    assert fake.send(job, machine_id=1) == first
    assert fake.depth() == 1
    clock.advance(seconds=2)   # outside the window SQS accepts it again; the worker's claim handles that
    fake.send(job, machine_id=1)
    assert fake.depth() == 2


def test_fake_delivers_one_message_per_group_at_a_time_in_order(fake):
    a1, a2, b1 = _job(), _job(), _job()
    fake.send(a1, machine_id=1)
    fake.send(a2, machine_id=1)
    fake.send(b1, machine_id=2)
    first = fake.receive()[0]
    second = fake.receive()[0]
    assert (first.job_id, second.job_id) == (a1, b1)   # machine 1 is blocked until a1 is done
    assert fake.receive() == []
    fake.delete(first.receipt_handle)
    assert fake.receive()[0].job_id == a2


def test_fake_redelivers_after_the_visibility_timeout_with_a_higher_count(fake, clock):
    job = _job()
    fake.send(job, machine_id=1)
    first = fake.receive()[0]
    clock.advance(seconds=59)
    assert fake.receive() == []
    clock.advance(seconds=2)
    again = fake.receive()[0]
    assert again.job_id == job and again.receive_count == 2 and again.receipt_handle != first.receipt_handle
    with pytest.raises(QueueError) as info:   # the first receipt expired with the redelivery
        fake.delete(first.receipt_handle)
    assert info.value.retryable is False


def test_fake_visibility_extension_keeps_a_slow_message_hidden(fake, clock):
    fake.send(_job(), machine_id=1)
    message = fake.receive()[0]
    clock.advance(seconds=50)
    fake.extend_visibility(message.receipt_handle, 60)
    clock.advance(seconds=30)
    assert fake.receive() == []


def test_fake_moves_a_message_to_the_dlq_after_max_receives(fake, clock):
    """maxReceiveCount 3: a message received three times and never deleted goes to the DLQ, unblocking the group."""
    poison, next_job = _job(), _job()
    fake.send(poison, machine_id=1)
    fake.send(next_job, machine_id=1)
    for expected in (1, 2, 3):
        message = fake.receive()[0]
        assert message.job_id == poison and message.receive_count == expected
        clock.advance(seconds=61)
    assert fake.receive()[0].job_id == next_job
    [dead] = fake.receive_dead_letters()
    assert dead.job_id == poison
    fake.delete_dead_letter(dead.receipt_handle)
    assert fake.receive_dead_letters() == []


def test_fake_can_fail_sends_like_sqs(fake):
    fake.fail_next("send", code="ServiceUnavailable")
    with pytest.raises(QueueError) as info:
        fake.send(_job(), machine_id=1)
    assert info.value.retryable is True and fake.depth() == 0
    fake.send(_job(), machine_id=1)   # next call works again
    assert fake.depth() == 1


def test_fake_can_fail_receive_and_delete(fake):
    fake.send(_job(), machine_id=1)
    fake.fail_next("receive", code="InternalError")
    with pytest.raises(QueueError):
        fake.receive()
    message = fake.receive()[0]
    fake.fail_next("delete", code="InternalError")
    with pytest.raises(QueueError):
        fake.delete(message.receipt_handle)
    fake.delete(message.receipt_handle)


# --- selection ------------------------------------------------------------------------------------------

def test_build_queue_is_off_by_default_so_phase1_is_unchanged():
    assert build_queue({"QUEUE_BACKEND": "none"}) is None
    assert build_queue({}) is None
    assert isinstance(build_queue({"QUEUE_BACKEND": "fake"}), FakeJobQueue)
    sqs_queue = build_queue({"QUEUE_BACKEND": "sqs", "SQS_QUEUE_URL": QUEUE_URL, "SQS_DLQ_URL": DLQ_URL,
                             "AWS_REGION": "ap-northeast-2"})
    assert isinstance(sqs_queue, SqsJobQueue)
    with pytest.raises(ValueError):
        build_queue({"QUEUE_BACKEND": "kafka"})


def test_config_exposes_queue_settings_with_queue_off():
    from app.config import Config as AppConfig

    assert AppConfig.QUEUE_BACKEND == "none"
    assert hasattr(AppConfig, "SQS_QUEUE_URL") and hasattr(AppConfig, "SQS_DLQ_URL")


# --- holding a dead letter out of sight (relay keeps unusable DLQ messages, decision 2026-11-02) -------------

def test_sqs_hold_dead_letter_changes_visibility_on_the_dlq(sqs):
    queue, stub = sqs
    stub.add_response("change_message_visibility", {}, {
        "QueueUrl": DLQ_URL, "ReceiptHandle": "drh-1", "VisibilityTimeout": 3600})
    queue.hold_dead_letter("drh-1", 3600)


def test_fake_held_dead_letter_is_hidden_until_the_hold_ends(fake, clock):
    poison = _job()
    fake.send(poison, machine_id=1)
    for _ in range(3):
        fake.receive()
        clock.advance(seconds=61)
    [dead] = fake.receive_dead_letters()
    fake.hold_dead_letter(dead.receipt_handle, 3600)
    assert fake.receive_dead_letters() == []
    clock.advance(seconds=3601)
    [again] = fake.receive_dead_letters()
    assert again.job_id == poison and again.receive_count == dead.receive_count + 1


# --- fast-fail send for the photo intake API (ADR 0019 follow-up, 9단계 finding) ---------------------------
# The ingest request sends once right after its commit. If the queue is down that single send must fail fast
# (the job stays pending_publish and the relay retries): boto3's normal 3 attempts x 5 s connect timeout held the
# request for ~39 s in the Compose test. Only the ingest send is fast; the relay and the worker keep their settings.

def test_fast_client_config_is_one_attempt_with_short_timeouts():
    queue = SqsJobQueue(QUEUE_URL, DLQ_URL, region="ap-northeast-2")
    fast, normal = queue.fast_client_config(), queue.client_config()
    assert fast.retries == {"mode": "standard", "total_max_attempts": 1}
    assert fast.connect_timeout <= 2 and fast.read_timeout <= 3
    assert normal.retries == {"mode": "standard", "total_max_attempts": 3} and normal.read_timeout > 20   # unchanged


def test_fast_send_uses_its_own_client_and_normal_calls_keep_the_normal_one(monkeypatch):
    built = []

    class Recorder:
        def __init__(self, config):
            self.config = config

        def send_message(self, **kwargs):
            return {"MessageId": "m-1"}

    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: built.append(kwargs["config"]) or Recorder(kwargs["config"]))
    queue = SqsJobQueue(QUEUE_URL, DLQ_URL, region="ap-northeast-2")
    assert queue.send(JOB, 7, fast=True) == "m-1"
    assert queue.send(JOB, 7) == "m-1"
    assert [c.retries["total_max_attempts"] for c in built] == [1, 3]
    queue.send(JOB, 7, fast=True)
    queue.send(JOB, 7)
    assert len(built) == 2                                   # each client is built once


def test_fast_send_with_an_injected_client_sends_the_same_message(sqs):
    queue, stub = sqs
    stub.add_response("send_message", {"MessageId": "m-1", "SequenceNumber": "1"}, {
        "QueueUrl": QUEUE_URL, "MessageBody": message_body(JOB),
        "MessageGroupId": "m-7", "MessageDeduplicationId": str(JOB)})
    assert queue.send(JOB, machine_id=7, fast=True) == "m-1"


def test_fake_queue_accepts_the_fast_flag(fake):
    fake.send(JOB, 7, fast=True)
    assert fake.depth() == 1


def _serve(behaviour):
    """A throw-away local TCP server. behaviour(conn) decides what it does with each connection. Returns (port, state)."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    state = {"connections": 0, "stop": False}

    def loop():
        server.settimeout(0.2)
        while not state["stop"]:
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            state["connections"] += 1
            threading.Thread(target=behaviour, args=(conn, state), daemon=True).start()
        server.close()

    threading.Thread(target=loop, daemon=True).start()
    return server.getsockname()[1], state


def _real_queue(port, monkeypatch):
    """A real boto3 client pointed at the local test server (the endpoint override is mandatory: without it boto3
    would resolve the regional AWS endpoint)."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_SQS", f"http://127.0.0.1:{port}")
    return SqsJobQueue(f"http://127.0.0.1:{port}/000000000000/pg-judgement.fifo", "", region="ap-northeast-2")


@pytest.fixture
def aws_dummy_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "local-test", "AWS_SECRET_ACCESS_KEY": "local-test"}.items():
        monkeypatch.setenv(k, v)


def _timed_send(queue):
    import time

    start = time.monotonic()
    with pytest.raises(QueueError) as info:
        queue.send(JOB, 7, fast=True)
    return time.monotonic() - start, info.value


def test_fast_send_connection_refused_fails_at_once(aws_dummy_env, monkeypatch):
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                            # nothing listens here now
    elapsed, error = _timed_send(_real_queue(port, monkeypatch))
    assert elapsed < 5 and error.retryable and error.code in ("EndpointConnectionError", "ConnectTimeoutError")   # bounded by connect_timeout (Windows retries a refused loopback connect for ~2 s)


def test_fast_send_gives_up_on_a_response_delay_within_the_read_timeout(aws_dummy_env, monkeypatch):
    import time

    def hang(conn, state):          # accepts and reads, never answers
        time.sleep(30)

    port, state = _serve(hang)
    try:
        queue = _real_queue(port, monkeypatch)
        elapsed, error = _timed_send(queue)
    finally:
        state["stop"] = True
    assert elapsed < 6 and error.retryable and error.code == "ReadTimeoutError"
    assert state["connections"] == 1                         # one attempt, no retries


def test_fast_send_with_a_lost_response_is_one_attempt_and_retryable(aws_dummy_env, monkeypatch):
    def read_then_close(conn, state):
        conn.recv(65536)
        conn.close()

    port, state = _serve(read_then_close)
    try:
        elapsed, error = _timed_send(_real_queue(port, monkeypatch))
    finally:
        state["stop"] = True
    assert elapsed < 3 and error.retryable
    assert state["connections"] == 1


def test_fast_send_connect_timeout_is_a_retryable_queue_error():
    from botocore.exceptions import ConnectTimeoutError

    class Blackhole:
        def send_message(self, **kwargs):
            raise ConnectTimeoutError(endpoint_url=QUEUE_URL)

    with pytest.raises(QueueError) as info:
        SqsJobQueue(QUEUE_URL, DLQ_URL, client=Blackhole()).send(JOB, 7, fast=True)
    assert info.value.retryable and info.value.code == "ConnectTimeoutError"


# --- FakeJobQueue: message retention (SQS drops a message once it is older than the retention period) -----

def test_fake_retention_drops_a_message_that_was_never_consumed(clock):
    """SQS counts retention from the send, whatever happens in between (receives do not reset it)."""
    queue = FakeJobQueue(clock=clock, retention=timedelta(days=4))
    job = _job()
    queue.send(job, machine_id=1)
    [message] = queue.receive()          # received, never deleted
    clock.advance(days=3, hours=23)
    assert queue.depth() == 1
    clock.advance(hours=2)
    assert queue.receive() == [] and queue.depth() == 0
    assert message.job_id == job


def test_fake_retention_applies_to_the_dead_letter_queue_from_the_move(clock):
    """A FIFO queue starts the retention clock again when the message moves to its DLQ (AWS docs; to be
    confirmed on AWS)."""
    queue = FakeJobQueue(clock=clock, visibility_timeout=60, max_receive_count=1, retention=timedelta(days=4))
    queue.send(_job(), machine_id=1)
    queue.receive()                                # received once, never deleted: max_receive_count reached
    clock.advance(days=3)
    assert queue.receive() == []                   # moved to the DLQ now (day 3)
    clock.advance(days=3)                          # day 6: 6 days after the send, 3 after the move
    assert len(queue.receive_dead_letters()) == 1
    clock.advance(days=2)                          # day 8: 5 days after the move
    assert queue.receive_dead_letters() == []


def test_fake_without_retention_keeps_messages_forever(fake, clock):
    fake.send(_job(), machine_id=1)
    clock.advance(days=30)
    assert fake.depth() == 1
