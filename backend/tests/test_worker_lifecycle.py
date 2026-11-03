"""Worker and relay life cycle: warm-up before the first receive (P5) and a clean stop on SIGTERM (P4), ADR 0019.

What a stop must guarantee
    1. No new work after the signal: nothing is received; a message that the long poll still returns is given back at once.
    2. The job in flight is finished (inference, verdict, side effects, delete), not abandoned.
    3. If the grace period runs out anyway (SIGKILL), nothing is lost: the lease (55 s) ends, the message comes back after the
       visibility timeout (60 s), another worker claims the job, and claim fencing drops the dead worker's late result.
The numbers belong together: lease 55 s < visibility timeout 60 s <= terminationGracePeriodSeconds 60 s.

Warm-up: `serve()` loads the models BEFORE its first receive; a load that fails raises and consumes nothing.
Nothing here touches AWS: the in-memory queue runs on a test clock, inference is scripted.
"""
import re
import signal
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from app import adapters
from app.adapters.inference import InferenceError
from app.adapters.queue import FakeJobQueue, QueueError
from app.cli import install_stop_handlers, restore_stop_handlers
from app.extensions import db
from app.models import DetectionJob, JobStatus
from app.services.relay import OutboxRelay
from app.services.worker import JudgementWorker

from .test_relay import _fail_sends
from .test_worker import GANGNAM, PANGYO, _results, app_overrides, clock, drain, env, send, worker  # noqa: F401


def _job(job_id):
    return db.session.get(DetectionJob, uuid.UUID(str(job_id)))


def _count_receives(queue):
    calls = []
    real = queue.receive

    def receive():
        calls.append(1)
        return real()

    queue.receive = receive
    return calls, real


def _boom():
    raise InferenceError("model load failed: out of memory")


# --- P5: warm-up before the first receive --------------------------------------------------------------------------------------

def test_the_models_are_loaded_before_the_first_receive(app, send, worker, env):
    send()
    events = []
    env.preload = lambda: events.append("preload") or {"load_seconds": 1.0}
    with app.app_context():
        queue = adapters.queue()
        real = queue.receive
        queue.receive = lambda: events.append("receive") or real()
        assert worker().serve(until_empty=True) == 1
    assert events[0] == "preload" and "receive" in events
    assert events.count("preload") == 1


def test_a_failed_preload_consumes_nothing_and_judges_nothing(app, send, worker, env):
    job_id = send()
    env.preload = _boom
    with app.app_context():
        queue = adapters.queue()
        calls, _ = _count_receives(queue)
        with pytest.raises(InferenceError):
            worker().serve(until_empty=True)
        assert calls == []                                   # the queue was never read
        assert queue.depth() == 1
        job = _job(job_id)
        assert job.status == JobStatus.QUEUED and job.claim_attempt == 0
        assert _results() == [] and env.calls == 0           # no verdict either


def test_the_cli_exits_with_an_error_and_a_clear_message_when_the_models_cannot_load(app, send, env):
    job_id = send()
    env.preload = _boom
    result = app.test_cli_runner().invoke(args=["judge-worker"])
    assert result.exit_code == 1
    assert "model" in result.output.lower()
    with app.app_context():
        assert adapters.queue().depth() == 1 and _job(job_id).claim_attempt == 0


def test_adapters_without_a_preload_method_are_simply_ready(app, send, worker, env):
    """Scripted/mock inference has no preload: serve() must not require one."""
    send()
    assert not hasattr(env, "preload")
    with app.app_context():
        assert worker().serve(until_empty=True) == 1


def test_the_preload_report_is_logged_for_measurement(app, send, worker, env, caplog):
    env.preload = lambda: {"attempts": 1, "load_seconds": 12.3, "first_inference_seconds": 1.8, "second_inference_seconds": 0.6}
    with caplog.at_level("INFO"):
        with app.app_context():
            worker().serve(until_empty=True)
    assert "load_seconds=12.3" in caplog.text and "first_inference_seconds=1.8" in caplog.text


# --- P4: stopping the worker -----------------------------------------------------------------------------------------------------

def test_a_stop_requested_before_the_loop_loads_and_receives_nothing(app, send, worker, env):
    send()
    env.preload = lambda: pytest.fail("a stopped worker must not even load the models")
    with app.app_context():
        w = worker()
        calls, _ = _count_receives(adapters.queue())
        w.request_stop()
        assert w.serve() == 0
        assert calls == [] and adapters.queue().depth() == 1


def test_a_stop_requested_while_the_models_load_ends_without_receiving(app, send, worker, env):
    """SIGTERM can arrive during the 10-35 s model load. After the load the worker exits cleanly instead of starting to consume."""
    job_id = send()
    with app.app_context():
        w = worker()
        env.preload = lambda: w.request_stop() or {}
        calls, _ = _count_receives(adapters.queue())
        assert w.serve() == 0
        assert calls == [] and _job(job_id).claim_attempt == 0


def test_a_stop_during_a_job_lets_it_finish_and_takes_no_more(app, send, worker, env):
    send(serial=GANGNAM, verdict="permitted")
    send(serial=PANGYO, verdict="permitted")     # another machine, so its message is deliverable at the same time
    with app.app_context():
        w = worker()
        real = env.analyze

        def analyze(image_bytes, filename=""):
            w.request_stop()                      # SIGTERM arrives while the first photo is being judged
            return real(image_bytes, filename)

        env.analyze = analyze
        assert w.serve(until_empty=True) == 1
        statuses = sorted(j.status for j in db.session.query(DetectionJob).all())
        assert statuses == [JobStatus.DONE, JobStatus.QUEUED]            # the one in flight finished, the other was not touched
        untouched = db.session.query(DetectionJob).filter_by(status=JobStatus.QUEUED).one()
        assert untouched.claim_attempt == 0
        assert len(_results()) == 1
        assert adapters.queue().depth() == 1                              # the finished job's message was deleted, the other one is still there


def test_a_message_received_after_the_stop_is_given_back_untouched(app, send, worker, env):
    """The long poll cannot be cancelled: a message may still come back after SIGTERM. It must not be claimed or judged, and it must not
    stay hidden for a visibility timeout."""
    job_id = send()
    with app.app_context():
        w = worker()
        queue = adapters.queue()
        real = queue.receive

        def receive():
            messages = real()
            w.request_stop()                      # the signal landed while the poll was waiting
            return messages

        queue.receive = receive
        assert w.serve() == 0
        job = _job(job_id)
        assert job.status == JobStatus.QUEUED and job.claim_attempt == 0 and env.calls == 0
        queue.receive = real
        again = queue.receive()                   # visible at once, no 60-second wait
        assert len(again) == 1 and again[0].receive_count == 2     # the give-back cost one receive: documented, maxReceiveCount is 3


def test_giving_a_message_back_survives_a_queue_error(app, send, worker, env, clock):
    send()
    with app.app_context():
        w = worker()
        queue = adapters.queue()
        real = queue.receive
        queue.receive = lambda: (lambda m: (w.request_stop(), m)[1])(real())
        queue.fail_next("extend_visibility")      # the give-back itself fails
        assert w.serve() == 0                     # no exception, the process still exits
        queue.receive = real
    assert drain(app, worker) == []               # still hidden...
    clock.advance(seconds=61)
    assert drain(app, worker) == ["done"]         # ...and back after the visibility timeout, judged once
    with app.app_context():
        assert len(_results()) == 1


def test_a_worker_killed_in_flight_leaves_the_job_to_another_after_the_visibility_timeout(app, send, worker, env, clock):
    """The grace period ran out (SIGKILL): the dead worker holds the claim, nobody deletes the message."""
    job_id = send()
    with app.app_context():
        message = adapters.queue().receive()[0]
        assert worker("dead")._claim(message.job_id) == 1
    assert drain(app, worker) == []               # hidden for 60 s, and the lease (55 s) is still valid
    clock.advance(seconds=61)
    assert drain(app, worker) == ["done"]
    with app.app_context():
        job = _job(job_id)
        assert job.status == JobStatus.DONE and job.claim_attempt == 2 and len(_results()) == 1


def test_lease_visibility_timeout_and_grace_period_fit_together():
    lease = JudgementWorker().lease.total_seconds()
    visibility = FakeJobQueue().visibility_timeout
    assert lease == 55 and visibility == 60 and lease < visibility
    infra = Path(__file__).resolve().parents[3] / "parking-guardian-infra" / "terraform" / "stacks" / "data" / "variables.tf"
    config = Path(__file__).resolve().parents[3] / "parking-guardian-config" / "apps" / "parking-guardian" / "worker-deployment.yaml"
    if infra.exists():       # the real queue: Terraform default visibility timeout
        body = infra.read_text(encoding="utf-8").split('variable "judgement_visibility_timeout_seconds"', 1)[1].split("\n}", 1)[0]
        assert int(re.search(r"default\s*=\s*(\d+)", body).group(1)) == visibility
    if config.exists():      # the Pod: grace period >= lease, so a claim that is still valid when SIGTERM arrives can finish
        grace = int(re.search(r"terminationGracePeriodSeconds:\s*(\d+)", config.read_text(encoding="utf-8")).group(1))
        assert grace >= lease
        assert grace >= 20 + 10      # the long poll that may still be running (20 s) plus one judgement (a few seconds)


# --- the signal handlers -------------------------------------------------------------------------------------------------------------

def test_sigterm_and_sigint_set_the_stop_event_and_the_old_handlers_come_back():
    stop = threading.Event()
    previous = install_stop_handlers(stop)
    try:
        signal.raise_signal(signal.SIGTERM)
        assert stop.is_set()
        stop.clear()
        signal.raise_signal(signal.SIGINT)               # Ctrl-C stops the same way, no KeyboardInterrupt traceback
        assert stop.is_set()
    finally:
        restore_stop_handlers(previous)
    assert signal.getsignal(signal.SIGTERM) == previous[signal.SIGTERM]
    assert signal.getsignal(signal.SIGINT) == previous[signal.SIGINT]


# --- the relay ---------------------------------------------------------------------------------------------------------------------

def _two_machines_with_pending_jobs(app, send, clock):
    restore = _fail_sends(app, 2)                 # ingest cannot reach the queue: both jobs stay pending_publish
    first, second = send(serial=GANGNAM), send(serial=PANGYO)
    restore()
    clock.advance(seconds=5)
    return first, second


def test_the_relay_stops_between_machines_and_leaves_no_half_sent_job(app, send, clock, env):
    first, second = _two_machines_with_pending_jobs(app, send, clock)
    stop = threading.Event()
    with app.app_context():
        queue = adapters.queue()
        real = queue.send

        def send_then_stop(job_id, machine_id, fast=False):
            result = real(job_id, machine_id, fast=fast)
            stop.set()                                    # SIGTERM right after the first send
            return result

        queue.send = send_then_stop
        assert OutboxRelay(clock=clock, stop=stop).publish_pending() == 1
        queue.send = real
        assert sorted([_job(first).status, _job(second).status]) == [JobStatus.PENDING_PUBLISH, JobStatus.QUEUED]
        assert queue.depth() == 1
        # the next relay (the new Pod) finishes the work; nothing is sent twice
        assert OutboxRelay(clock=clock).publish_pending() == 1
        assert queue.depth() == 2
        assert {_job(first).status, _job(second).status} == {JobStatus.QUEUED}


def test_a_relay_that_is_told_to_stop_does_not_start_a_pass(app, send, clock, env):
    first, _ = _two_machines_with_pending_jobs(app, send, clock)
    stop = threading.Event()
    stop.set()
    with app.app_context():
        stats = OutboxRelay(clock=clock, stop=stop).run_once()
        assert stats["stopped"] is True and stats["published"] == 0
        assert _job(first).status == JobStatus.PENDING_PUBLISH and adapters.queue().depth() == 0


def test_the_relay_loop_wakes_from_its_sleep_when_stopped(app, clock, env):
    stop = threading.Event()
    result = {}

    def run():
        with app.app_context():
            result["passes"] = OutboxRelay(clock=clock, stop=stop).run_loop(interval=30.0)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    started = time.time()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and time.time() - started < 3     # not 30 seconds
    assert result["passes"] >= 1
