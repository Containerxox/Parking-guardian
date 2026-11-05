"""Observability stage 3: worker metrics (pg_worker_messages_total, pg_worker_process_seconds, pg_job_e2e_seconds, pg_queue_wait_seconds).

Same harness as test_worker.py (real ingest, in-memory FIFO on a test clock, scripted inference). Nothing touches AWS. Pins:
a verdict is counted only AFTER its transaction committed, once per job (retries and redeliveries are not counted again), unknown or
negative times are skipped (not recorded as 0), a failing metric never changes what the worker does, and no label carries an id.
"""
import uuid
from datetime import datetime, timezone

import pytest
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from app import adapters, metrics
from app.adapters.queue import QueueMessage, _sent_at
from app.extensions import db
from app.models import DetectionJob
from app.services.worker import JudgementWorker

from .test_worker import app_overrides, clock, drain, env, send, worker  # noqa: F401


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Snap:
    """Counter deltas since creation (the registry is process-wide, so tests compare against a baseline)."""

    def __init__(self):
        self.base = self._now()

    @staticmethod
    def _now():
        out = {}
        for outcome in ("done", "stale", "effects_retry", "rejected", "retry", "already", "busy", "lost_claim", "isolated"):
            out[outcome] = val("pg_worker_messages_total", outcome=outcome)
        out["e2e"] = val("pg_job_e2e_seconds_count")
        out["e2e_sum"] = val("pg_job_e2e_seconds_sum")
        out["wait"] = val("pg_queue_wait_seconds_count")
        for metric in ("e2e", "queue_wait"):
            for reason in ("missing", "negative"):
                out[f"skip_{metric}_{reason}"] = val("pg_worker_timing_skipped_total", metric=metric, reason=reason)
        return out

    def delta(self):
        now = self._now()
        return {k: round(now[k] - self.base[k], 6) for k in now}


def test_done_counts_once_after_commit_with_e2e_and_queue_wait(app, send, worker, clock):
    snap = Snap()
    send()
    clock.advance(seconds=4)                       # 4 s pass between the accepted photo and the worker
    assert drain(app, worker) == ["done"]
    d = snap.delta()
    assert d["done"] == 1 and d["e2e"] == 1 and d["wait"] == 1
    assert 4 <= d["e2e_sum"] < 8                   # received_at (real clock) -> worker clock after the commit
    assert val("pg_worker_process_seconds_count", outcome="done") >= 1
    assert sum(v for k, v in d.items() if k.startswith("skip_")) == 0


def test_stale_job_also_records_e2e(app, send, worker):
    import uuid as _uuid
    a, b = _uuid.uuid4(), _uuid.uuid4()
    send(session=a, seq=1)
    send(session=b, seq=1)
    snap = Snap()
    assert drain(app, worker) == ["done", "done"]
    assert snap.delta()["e2e"] == 2


def test_inference_failure_counts_retry_and_no_verdict_metrics(app, send, worker, env):
    snap = Snap()
    send()
    env.fail_times = 1
    assert drain(app, worker) == ["retry"]
    d = snap.delta()
    assert d["retry"] == 1 and d["done"] == 0 and d["e2e"] == 0
    assert d["wait"] == 1                           # the queue wait of the first delivery is recorded even though it failed


def test_retry_then_success_counts_e2e_and_queue_wait_once(app, send, worker, env, clock):
    snap = Snap()
    send()
    env.fail_times = 1
    assert drain(app, worker) == ["retry"]
    clock.advance(seconds=61)                       # visibility timeout and lease end: redelivery
    assert drain(app, worker) == ["done"]
    d = snap.delta()
    assert d["retry"] == 1 and d["done"] == 1
    assert d["e2e"] == 1                            # one photo = one E2E sample
    assert d["wait"] == 1                           # the redelivery (receive_count 2) is not a queue wait sample


def test_database_failure_before_commit_counts_no_verdict(app, send, worker, monkeypatch):
    from app.services import worker as worker_module

    snap = Snap()
    send()

    def explode(*args, **kwargs):
        raise RuntimeError("database went away mid-transaction")

    monkeypatch.setattr(worker_module, "_finish_job", explode)
    assert drain(app, worker) == ["retry"]
    d = snap.delta()
    assert d["retry"] == 1 and d["done"] == 0 and d["stale"] == 0 and d["effects_retry"] == 0 and d["e2e"] == 0


def test_effects_failure_after_commit_is_effects_retry_and_not_counted_twice(app, send, worker, clock, monkeypatch):
    from app.services import worker as worker_module

    a = uuid.uuid4()
    send(session=a, seq=1)
    drain(app, worker)
    send(session=a, seq=2, event="confirm")        # this one creates the violation, so effects run
    real = worker_module.JudgementWorker._run_effects

    def crash(self, job_id):
        raise RuntimeError("killed after commit")

    snap = Snap()
    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects", crash)
    assert drain(app, worker) == ["retry"]
    d = snap.delta()
    assert d["effects_retry"] == 1 and d["retry"] == 0 and d["e2e"] == 1    # verdict is stored: E2E counted at the commit
    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects", real)
    clock.advance(seconds=61)
    assert drain(app, worker) == ["already"]
    d = snap.delta()
    assert d["already"] == 1 and d["done"] == 0 and d["e2e"] == 1           # redelivery adds nothing to done / E2E


def test_rejected_has_no_e2e(app, send, worker):
    snap = Snap()
    send(verdict="broken")
    assert drain(app, worker) == ["rejected"]
    d = snap.delta()
    assert d["rejected"] == 1 and d["e2e"] == 0 and d["done"] == 0


def test_clock_behind_the_timestamps_is_skipped_not_recorded_as_zero(app, send, worker, clock):
    snap = Snap()
    send()
    clock.advance(seconds=-120)                     # worker clock before the SQS send time and before received_at
    assert drain(app, worker) == ["done"]
    d = snap.delta()
    assert d["done"] == 1
    assert d["e2e"] == 0 and d["wait"] == 0
    assert d["skip_e2e_negative"] == 1 and d["skip_queue_wait_negative"] == 1


def test_missing_sent_timestamp_is_skipped(app, send, worker):
    snap = Snap()
    job_id = send()
    with app.app_context():
        adapters.queue()._messages.clear()          # deliver the job by hand, as a message that carries no SentTimestamp
        w = worker()
        message = QueueMessage(job_id=uuid.UUID(job_id), receipt_handle="manual", receive_count=1, message_id="m", group_id=None, body="")
        assert w.process(message) == "done"
    d = snap.delta()
    assert d["wait"] == 0 and d["skip_queue_wait_missing"] == 1 and d["e2e"] == 1


def test_a_failing_metric_never_changes_the_outcome(app, send, worker, monkeypatch):
    snap = Snap()
    send()

    def boom(*args, **kwargs):
        raise RuntimeError("metrics are broken")

    monkeypatch.setattr(metrics.JOB_E2E, "observe", boom)
    monkeypatch.setattr(metrics.QUEUE_WAIT, "observe", boom)
    monkeypatch.setattr(metrics.WORKER_PROCESS_SECONDS, "labels", boom)
    assert drain(app, worker) == ["done"]
    with app.app_context():
        assert db.session.scalars(db.select(DetectionJob.status)).one() == "done"
    assert snap.delta()["done"] == 1


def test_sent_timestamp_parsing():
    assert _sent_at({"SentTimestamp": "1793904000000"}) == datetime(2026, 11, 5, 18, 40, tzinfo=timezone.utc)
    assert _sent_at({}) is None
    assert _sent_at({"SentTimestamp": "not-a-number"}) is None


def test_sqs_receive_asks_for_the_sent_timestamp():
    from app.adapters.queue import SqsJobQueue

    seen = {}

    class Client:
        def receive_message(self, **kwargs):
            seen.update(kwargs)
            return {"Messages": [{"MessageId": "m1", "ReceiptHandle": "r", "Body": '{"job_id": "%s"}' % uuid.uuid4(),
                                  "Attributes": {"ApproximateReceiveCount": "1", "MessageGroupId": "g", "SentTimestamp": "1793904000000"}}]}

    queue = SqsJobQueue("https://example/q.fifo", client=Client(), dlq_url="https://example/dlq.fifo")
    [message] = queue.receive()
    assert "SentTimestamp" in seen["MessageSystemAttributeNames"]
    assert message.sent_at == datetime(2026, 11, 5, 18, 40, tzinfo=timezone.utc)


def test_labels_are_small_fixed_sets_without_ids(app, send, worker):
    send()
    drain(app, worker)
    allowed = {"outcome", "metric", "reason", "le", "stage", "verdict", "method", "route", "status", "operation", "queue", "state",
               "reason", "pg_inference_model_state"}
    families = {f.name: f for f in text_string_to_metric_families(generate_latest(REGISTRY).decode())}
    for name in ("pg_worker_messages", "pg_worker_process_seconds", "pg_job_e2e_seconds", "pg_queue_wait_seconds",
                 "pg_worker_timing_skipped"):
        family = families[name]
        for sample in family.samples:
            assert set(sample.labels) <= allowed, (name, sample.labels)
    assert len({s.labels["outcome"] for s in families["pg_worker_messages"].samples}) <= 12


def test_histogram_buckets_cover_the_10_second_target():
    bounds = [float(s.labels["le"]) for f in text_string_to_metric_families(generate_latest(REGISTRY).decode())
              if f.name == "pg_job_e2e_seconds" for s in f.samples if s.name.endswith("_bucket") and s.labels["le"] != "+Inf"]
    assert 10.0 in bounds and min(bounds) <= 0.5 and max(bounds) >= 300
