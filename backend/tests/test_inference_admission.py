"""Concurrency limits of the inference service (design doc 3.5, 4.2 T1).

Two different numbers, kept apart:
  running  = photos inside the model right now. Fixed at 1: the model lock stays (not proven thread safe).
  waiting  = photos queued behind it, at most INFERENCE_MAX_WAITING (default 2).
A photo that would be the (max_waiting + 1)-th waiter is refused at once: 503 {"error": "busy"} + Retry-After: 1.
Everything runs on real threads against the Flask test client, so the limit is shown to hold, not assumed.
"""
import threading
import time

import pytest
from prometheus_client import REGISTRY

from app.adapters.inference import InferenceError, InferenceResult, InvalidImageError
from inference_service import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-content"


class BlockingModel:
    """Holds every photo inside analyze() until `release` is set, and records how many ran at once."""
    name = "blocking"
    state = "ready"

    def __init__(self):
        self.release = threading.Event()
        self._mutex = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def warm_up(self):
        pass

    def analyze(self, image_bytes, filename=""):
        with self._mutex:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            assert self.release.wait(10), "test never released the model"
        finally:
            with self._mutex:
                self.active -= 1
        return InferenceResult(has_window=True, window={"coords": [0, 0, 1, 1], "confidence": 0.9},
                               elapsed_ms=1, backend=self.name)


class FailingModel:
    name = "failing"
    state = "ready"

    def __init__(self, error):
        self.error = error

    def warm_up(self):
        pass

    def analyze(self, image_bytes, filename=""):
        raise self.error


def sample(name):
    return REGISTRY.get_sample_value(name) or 0.0


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def start_service(model, max_waiting, **extra):
    service = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock", "INFERENCE_MAX_WAITING": max_waiting,
                          **extra}, inference=model)
    return service, service.extensions["pg_gate"]


def send(service, out, index):
    response = service.test_client().post("/v1/analyze", data=JPEG, headers={"X-Filename": "a.jpg"},
                                          content_type="application/octet-stream")
    out[index] = response


def test_one_running_two_waiting_and_the_next_is_refused_at_once():
    model = BlockingModel()
    service, gate = start_service(model, max_waiting=2)
    rejected_before = sample("pg_inference_rejected_total")
    results = {}
    threads = [threading.Thread(target=send, args=(service, results, i)) for i in range(3)]
    for t in threads:
        t.start()
    assert wait_for(lambda: gate.running == 1 and gate.waiting == 2), (gate.running, gate.waiting)

    # running and waiting are separate numbers, also in the metrics
    assert sample("pg_inference_running") == 1
    assert sample("pg_inference_waiting") == 2

    started = time.perf_counter()
    fourth = service.test_client().post("/v1/analyze", data=JPEG, content_type="application/octet-stream")
    refused_in = time.perf_counter() - started

    assert fourth.status_code == 503
    assert fourth.get_json() == {"error": "busy"}
    assert fourth.headers["Retry-After"] == "1"
    assert refused_in < 1.0  # refused, not queued behind a model that is held
    assert model.calls == 1  # the refused photo never reached the model
    assert sample("pg_inference_rejected_total") == rejected_before + 1

    model.release.set()
    for t in threads:
        t.join(10)
    assert [results[i].status_code for i in range(3)] == [200, 200, 200]
    assert model.calls == 3
    assert model.max_active == 1  # one at a time, always
    assert (gate.running, gate.waiting) == (0, 0)
    assert sample("pg_inference_running") == 0
    assert sample("pg_inference_waiting") == 0

    # nothing leaked: the service takes photos again
    assert service.test_client().post("/v1/analyze", data=JPEG).status_code == 200


def test_many_more_requests_than_the_limit_never_run_in_parallel():
    model = BlockingModel()
    service, gate = start_service(model, max_waiting=2)
    results = {}
    threads = [threading.Thread(target=send, args=(service, results, i)) for i in range(10)]
    for t in threads:
        t.start()
    assert wait_for(lambda: len(results) == 7), len(results)  # 10 - (1 running + 2 waiting) refused immediately
    assert all(r.status_code == 503 and r.get_json() == {"error": "busy"} for r in results.values())

    model.release.set()
    for t in threads:
        t.join(10)
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200] * 3 + [503] * 7
    assert model.max_active == 1
    assert model.calls == 3


def test_zero_waiting_means_a_second_photo_is_refused_while_one_runs():
    model = BlockingModel()
    service, gate = start_service(model, max_waiting=0)
    results = {}
    first = threading.Thread(target=send, args=(service, results, 0))
    first.start()
    assert wait_for(lambda: gate.running == 1)

    second = service.test_client().post("/v1/analyze", data=JPEG)

    assert second.status_code == 503 and second.get_json() == {"error": "busy"}
    model.release.set()
    first.join(10)
    assert results[0].status_code == 200


@pytest.mark.parametrize("error, status", [(InferenceError("boom"), 503), (InvalidImageError("bad"), 400),
                                           (RuntimeError("unexpected"), 500)])
def test_the_slot_is_given_back_when_the_model_raises(error, status):
    service, gate = start_service(FailingModel(error), max_waiting=0)

    for _ in range(3):  # with max_waiting 0 a leaked slot would turn the second call into 'busy'
        assert service.test_client().post("/v1/analyze", data=JPEG).status_code == status
    assert (gate.running, gate.waiting) == (0, 0)


def test_default_limit_is_two_waiting():
    service = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock"})

    assert service.extensions["pg_gate"].max_waiting == 2


def test_fault_delay_slows_each_photo_inside_the_limit():
    """INFERENCE_FAULT_DELAY_MS is for tests and the local failure drill; it is 0 by default."""
    service, _ = start_service(BlockingModel(), max_waiting=2, INFERENCE_FAULT_DELAY_MS=150)
    service.extensions["pg_inference"].release.set()

    started = time.perf_counter()
    response = service.test_client().post("/v1/analyze", data=JPEG)

    assert response.status_code == 200
    assert time.perf_counter() - started >= 0.15


def test_fault_delay_is_off_by_default():
    service = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock"})

    assert service.config["INFERENCE_FAULT_DELAY_MS"] == 0
