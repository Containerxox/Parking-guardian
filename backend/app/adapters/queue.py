"""Judgement queue (Phase 2, ADR 0019 decisions 2, 3, 7; design 6.3, 6.7, 6.8, 6.11).

The database is the source of truth; the queue only carries "job <id> is ready to judge". So the contract is
small and every failure is reported, never hidden:

    send(job_id, machine_id) -> message id     MessageGroupId "m-<machine_id>", deduplication id = job_id
    receive() -> [QueueMessage]                 long poll 20 s, at most 1 message (the worker handles one)
    extend_visibility(receipt, seconds)         keep a slow message hidden while inference runs
    delete(receipt)                             after the job is done / stale / rejected
    receive_dead_letters() / delete_dead_letter(receipt)   the relay moves DLQ messages into the database
    hold_dead_letter(receipt, seconds)          keep a DLQ message but hide it (unusable messages stay in the DLQ)

Every failure raises QueueError. `retryable` says whether waiting can help; callers never drop a job on
failure (the job stays pending_publish and the relay retries with backoff, design 6.7). The adapter does not
retry beyond a bounded number of attempts inside one boto3 call.

Two implementations: SqsJobQueue (SQS FIFO) and FakeJobQueue (in memory, for local runs and tests).
QUEUE_BACKEND=none (the default) builds nothing, so the Phase 1 synchronous path is unchanged.
"""
import itertools
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

RECEIVE_WAIT_SECONDS = 20          # design 6.8: long poll 20 s
DEDUPLICATION_WINDOW = timedelta(minutes=5)   # SQS FIFO deduplication interval

# Error codes that mean "try again later". Everything else (permissions, missing queue, bad parameters,
# expired receipt handle) will fail the same way again until someone changes something.
RETRYABLE_CODES = frozenset({
    "ThrottlingException", "RequestThrottled", "Throttling", "ServiceUnavailable", "InternalError",
    "InternalFailure", "KmsThrottled", "RequestTimeout",
})


def group_id_for(machine_id: int) -> str:
    """Design 6.3: the order unit is the machine (owner of the episode state). No company or lot ids."""
    return f"m-{machine_id}"


def message_body(job_id: uuid.UUID) -> str:
    """Only the job id travels; the job row in the database holds everything else."""
    return json.dumps({"job_id": str(job_id)})


def _job_id_from(body: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(json.loads(body)["job_id"])
    except (ValueError, TypeError, KeyError):
        return None


class QueueError(Exception):
    def __init__(self, operation: str, code: str, message: str = "", retryable: bool = False):
        super().__init__(f"queue {operation} failed: {code} {message}".strip())
        self.operation = operation
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class QueueMessage:
    job_id: uuid.UUID | None   # None when the body could not be read; the worker logs and deletes it
    receipt_handle: str
    receive_count: int
    message_id: str
    group_id: str | None
    body: str
    sent_at: datetime | None = None   # SQS SentTimestamp (first send; a redelivery keeps it). None when unknown. Observability only.


# --- SQS --------------------------------------------------------------------------------------------------

def _sent_at(attributes: dict) -> datetime | None:
    """SentTimestamp is epoch milliseconds as a string. Missing or unreadable -> None (the caller then records no queue wait)."""
    try:
        return datetime.fromtimestamp(int(attributes["SentTimestamp"]) / 1000, tz=timezone.utc)
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None


class SqsJobQueue:
    """SQS FIFO queue. `client` is injectable for tests; otherwise boto3 is imported on first use, so the
    local and Phase 1 configurations never need SQS settings."""

    def __init__(self, queue_url: str, dlq_url: str = "", client=None, region: str | None = None,
                 max_attempts: int = 3):
        if not queue_url:
            raise ValueError("SQS_QUEUE_URL is required when QUEUE_BACKEND=sqs")
        if not queue_url.endswith(".fifo"):
            raise ValueError("SQS_QUEUE_URL must be a FIFO queue (design 6.3: order per machine)")
        self.queue_url = queue_url
        self.dlq_url = dlq_url
        self._client = client
        self._region = region
        self._max_attempts = max_attempts
        self._fast_client = None

    def client_config(self):
        from botocore.config import Config

        # read_timeout above the 20 s long poll, short connect timeout, bounded retries inside one call
        return Config(connect_timeout=5, read_timeout=RECEIVE_WAIT_SECONDS + 10,
                      retries={"mode": "standard", "total_max_attempts": self._max_attempts})

    def fast_client_config(self):
        from botocore.config import Config

        # Intake send only: no boto3 retry inside the call, connect 2 s, read 3 s (a send is a few KB).
        return Config(connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 1})

    @property
    def fast_client(self):
        if self._client is not None:   # injected (tests): one client for everything
            return self._client
        if self._fast_client is None:
            import boto3

            kwargs = {"config": self.fast_client_config()}
            if self._region:
                kwargs["region_name"] = self._region
            self._fast_client = boto3.client("sqs", **kwargs)
        return self._fast_client

    @property
    def client(self):
        if self._client is None:
            import boto3  # imported lazily: only the sqs backend needs it

            kwargs = {"config": self.client_config()}
            if self._region:
                kwargs["region_name"] = self._region
            self._client = boto3.client("sqs", **kwargs)
        return self._client

    def _call(self, operation: str, method: str, client=None, **params):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(client or self.client, method)(**params)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "ClientError")
            raise QueueError(operation, code, error.get("Message", ""), code in RETRYABLE_CODES) from exc
        except BotoCoreError as exc:   # connection refused, DNS, timeouts: the network, not the request
            raise QueueError(operation, type(exc).__name__, str(exc), retryable=True) from exc

    def send(self, job_id: uuid.UUID, machine_id: int, fast: bool = False) -> str:
        """`fast=True` is for the photo intake request only: one attempt, short timeouts, so a dead queue costs the
        device a few seconds instead of ~39 s. The job is already committed (pending_publish); the relay, which sends
        with the normal client and its own backoff, publishes it later. A lost response is harmless: the relay's
        resend is deduplicated by job id."""
        client = self.fast_client if fast else self.client
        response = self._call("send", "send_message", client=client, QueueUrl=self.queue_url,
                              MessageBody=message_body(job_id), MessageGroupId=group_id_for(machine_id),
                              MessageDeduplicationId=str(job_id))
        return response["MessageId"]

    def _receive(self, operation: str, url: str, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
        response = self._call(operation, "receive_message", QueueUrl=url, MaxNumberOfMessages=max_messages,
                              WaitTimeSeconds=wait_seconds,
                              MessageSystemAttributeNames=["ApproximateReceiveCount", "MessageGroupId", "SentTimestamp"])
        messages = []
        for raw in response.get("Messages", []):
            attributes = raw.get("Attributes", {})
            messages.append(QueueMessage(
                job_id=_job_id_from(raw.get("Body", "")), receipt_handle=raw["ReceiptHandle"],
                receive_count=int(attributes.get("ApproximateReceiveCount", "1")), message_id=raw["MessageId"],
                group_id=attributes.get("MessageGroupId"), body=raw.get("Body", ""), sent_at=_sent_at(attributes)))
        return messages

    def receive(self) -> list[QueueMessage]:
        return self._receive("receive", self.queue_url, 1, RECEIVE_WAIT_SECONDS)

    def depths(self) -> dict:
        """Approximate message counts of the main queue and the DLQ (observability only): {"main": {"visible", "not_visible", "delayed"}, "dlq": {...}}.
        Uses the fast client (connect 2 s, read 3 s, one attempt) so a slow SQS cannot hold the caller; any failure raises QueueError.
        Needs sqs:GetQueueAttributes on both queues. The DLQ part is left out when no DLQ is configured."""
        names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"]
        out = {}
        for label, url in (("main", self.queue_url), ("dlq", self.dlq_url)):
            if not url:
                continue
            attributes = self._call("get_queue_attributes", "get_queue_attributes", client=self.fast_client,
                                    QueueUrl=url, AttributeNames=names).get("Attributes", {})
            out[label] = {"visible": int(attributes.get(names[0], 0)), "not_visible": int(attributes.get(names[1], 0)),
                          "delayed": int(attributes.get(names[2], 0))}
        return out

    def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        self._call("extend_visibility", "change_message_visibility", QueueUrl=self.queue_url,
                   ReceiptHandle=receipt_handle, VisibilityTimeout=seconds)

    def delete(self, receipt_handle: str) -> None:
        self._call("delete", "delete_message", QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    def _require_dlq(self):
        if not self.dlq_url:
            raise ValueError("SQS_DLQ_URL is required to read dead letters")

    def receive_dead_letters(self, max_messages: int = 10) -> list[QueueMessage]:
        self._require_dlq()
        return self._receive("receive_dead_letters", self.dlq_url, max_messages, 0)

    def delete_dead_letter(self, receipt_handle: str) -> None:
        self._require_dlq()
        self._call("delete_dead_letter", "delete_message", QueueUrl=self.dlq_url, ReceiptHandle=receipt_handle)

    def hold_dead_letter(self, receipt_handle: str, seconds: int) -> None:
        """Keep a DLQ message but hide it for `seconds` (SQS allows up to 12 h), so the relay does not re-read it
        on every pass. Used for messages the relay must preserve (unreadable, unknown job, job still running)."""
        self._require_dlq()
        self._call("hold_dead_letter", "change_message_visibility", QueueUrl=self.dlq_url,
                   ReceiptHandle=receipt_handle, VisibilityTimeout=seconds)


# --- in-memory fake ------------------------------------------------------------------------------------

@dataclass
class _Stored:
    job_id_text: str
    body: str
    group_id: str
    message_id: str
    receive_count: int = 0
    visible_at: datetime | None = None
    receipt_handle: str | None = None
    enqueued_at: datetime | None = None   # retention clock: the send, or the move to the DLQ (FIFO restarts it)


class FakeJobQueue:
    """In-memory SQS FIFO with what later steps depend on: deduplication window, one in-flight message per
    group in send order, visibility timeout, receive count, redrive to a DLQ after max_receive_count.
    `clock` is injectable so tests can move time. Thread-safe enough for a local worker and API in one process.
    `retention` (default None = keep forever) drops a message once it is that old, like SQS's message retention
    period: counted from the send (receives do not reset it) and, in the DLQ, from the move.
    """

    def __init__(self, clock=None, visibility_timeout: int = 60, max_receive_count: int = 3,
                 dead_letter_visibility_timeout: int = 30, retention: timedelta | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.visibility_timeout = visibility_timeout
        self.dead_letter_visibility_timeout = dead_letter_visibility_timeout   # SQS default for a new queue
        self.max_receive_count = max_receive_count
        self.retention = retention
        self._messages: list[_Stored] = []
        self._dead: list[_Stored] = []
        self._dedup: dict[str, tuple[datetime, str]] = {}
        self._failures: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def fail_next(self, operation: str, code: str = "ServiceUnavailable") -> None:
        """Make the next call of `operation` (send / receive / delete / extend_visibility) fail like SQS."""
        self._failures[operation] = code

    def _maybe_fail(self, operation: str) -> None:
        code = self._failures.pop(operation, None)
        if code:
            raise QueueError(operation, code, "injected", code in RETRYABLE_CODES)

    def _expire(self, now: datetime) -> None:
        if self.retention is None:
            return
        self._messages = [m for m in self._messages if m.enqueued_at is None or now - m.enqueued_at < self.retention]
        self._dead = [m for m in self._dead if m.enqueued_at is None or now - m.enqueued_at < self.retention]

    def depth(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._messages)

    def depths(self) -> dict:
        """Same shape as SqsJobQueue.depths(): visible = deliverable now, not_visible = in flight (visibility timeout / held), delayed = 0."""
        with self._lock:
            self._maybe_fail("get_queue_attributes")
            now = self._clock()
            self._expire(now)
            self._redrive_exhausted(now)

            def split(items):
                hidden = sum(1 for m in items if m.visible_at is not None and m.visible_at > now)
                return {"visible": len(items) - hidden, "not_visible": hidden, "delayed": 0}
            return {"main": split(self._messages), "dlq": split(self._dead)}

    def send(self, job_id: uuid.UUID, machine_id: int, fast: bool = False) -> str:
        with self._lock:
            self._maybe_fail("send")
            now = self._clock()
            key = str(job_id)
            seen = self._dedup.get(key)
            if seen and now - seen[0] < DEDUPLICATION_WINDOW:
                return seen[1]
            message_id = f"fake-{next(self._ids)}"
            self._dedup[key] = (now, message_id)
            self._messages.append(_Stored(key, message_body(job_id), group_id_for(machine_id), message_id,
                                          enqueued_at=now))
            return message_id

    def _redrive_exhausted(self, now: datetime) -> None:
        for stored in list(self._messages):
            visible = stored.visible_at is None or stored.visible_at <= now
            if visible and stored.receive_count >= self.max_receive_count:
                self._messages.remove(stored)
                stored.receipt_handle, stored.visible_at, stored.receive_count = None, None, 0
                stored.enqueued_at = now
                self._dead.append(stored)

    def receive(self) -> list[QueueMessage]:
        with self._lock:
            self._maybe_fail("receive")
            now = self._clock()
            self._expire(now)
            self._redrive_exhausted(now)
            blocked = {m.group_id for m in self._messages if m.visible_at is not None and m.visible_at > now}
            for stored in self._messages:   # send order; only the head of each group can be delivered
                if stored.group_id in blocked:
                    continue
                if stored.visible_at is not None and stored.visible_at > now:
                    continue
                stored.receive_count += 1
                stored.visible_at = now + timedelta(seconds=self.visibility_timeout)
                stored.receipt_handle = uuid.uuid4().hex
                return [self._to_message(stored)]
            return []

    @staticmethod
    def _to_message(stored: _Stored) -> QueueMessage:
        return QueueMessage(job_id=_job_id_from(stored.body), receipt_handle=stored.receipt_handle,
                            receive_count=stored.receive_count, message_id=stored.message_id,
                            group_id=stored.group_id, body=stored.body, sent_at=stored.enqueued_at)

    def _find(self, receipt_handle: str, operation: str) -> _Stored:
        now = self._clock()
        for stored in self._messages:
            if stored.receipt_handle == receipt_handle and stored.visible_at and stored.visible_at > now:
                return stored
        raise QueueError(operation, "ReceiptHandleIsInvalid", "unknown or expired receipt handle", False)

    def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        with self._lock:
            self._maybe_fail("extend_visibility")
            self._find(receipt_handle, "extend_visibility").visible_at = self._clock() + timedelta(seconds=seconds)

    def delete(self, receipt_handle: str) -> None:
        with self._lock:
            self._maybe_fail("delete")
            self._messages.remove(self._find(receipt_handle, "delete"))

    def receive_dead_letters(self, max_messages: int = 10) -> list[QueueMessage]:
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._redrive_exhausted(now)
            batch = [m for m in self._dead if m.visible_at is None or m.visible_at <= now][:max_messages]
            for stored in batch:
                stored.receive_count += 1
                stored.receipt_handle = uuid.uuid4().hex
                stored.visible_at = now + timedelta(seconds=self.dead_letter_visibility_timeout)
            return [self._to_message(stored) for stored in batch]

    def _find_dead(self, receipt_handle: str, operation: str) -> _Stored:
        for stored in self._dead:
            if stored.receipt_handle == receipt_handle:
                return stored
        raise QueueError(operation, "ReceiptHandleIsInvalid", "unknown receipt handle", False)

    def delete_dead_letter(self, receipt_handle: str) -> None:
        with self._lock:
            self._dead.remove(self._find_dead(receipt_handle, "delete_dead_letter"))

    def hold_dead_letter(self, receipt_handle: str, seconds: int) -> None:
        with self._lock:
            self._find_dead(receipt_handle, "hold_dead_letter").visible_at = self._clock() + timedelta(seconds=seconds)


def build_queue(config):
    """QUEUE_BACKEND: none (default, Phase 1: no queue) | fake (in memory) | sqs (SQS FIFO)."""
    backend = (config.get("QUEUE_BACKEND") or "none").lower()
    if backend == "none":
        return None
    if backend == "fake":
        return FakeJobQueue()
    if backend == "sqs":
        return SqsJobQueue(config.get("SQS_QUEUE_URL", ""), config.get("SQS_DLQ_URL", ""),
                           region=config.get("AWS_REGION"))
    raise ValueError(f"unknown QUEUE_BACKEND: {backend!r} (expected none, fake or sqs)")
