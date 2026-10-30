"""HttpInference: the API side of the API / inference split (design doc 3.4, ADR 0018).

What happens to each kind of failure (one table, tested row by row below):

  inference service answers             retried?  becomes
  200 + valid body                      -         InferenceResult
  400 invalid_image                     no        InvalidImageError  (the photo is bad; sending it again changes nothing)
  503 busy                              yes*      InferenceError after the retries are used up
  connection refused / connect timeout  yes*      InferenceError after the retries are used up
  503 model_unavailable                 no        InferenceError     (the service said it cannot judge)
  read timeout (no answer in time)      no        InferenceError     (the photo may be half processed; resending doubles load)
  other 4xx/5xx, bad JSON, a verdict that contradicts itself    no    InferenceError
  bulkhead full (too many calls in flight)    no  InferenceError, nothing is sent

  * at most `retries` more attempts (default 1), only while the overall `deadline` still has room for the wait.

Most tests drive the real inference_service app through a stand-in for requests.Session (no network, fake clock);
the last block talks to it over real sockets.
"""
import threading
import time
from urllib.parse import urlparse

import pytest
import requests
import urllib3
from prometheus_client import REGISTRY
from werkzeug.serving import make_server

from app.adapters.inference import HttpInference, InferenceError, InferenceResult, InvalidImageError, MockInference
from inference_service import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-content"
BASE = "http://inference:8000"


class Clock:
    """Fake time: sleeping just moves the clock, so retry waits cost nothing."""

    def __init__(self):
        self.now_value = 1000.0
        self.sleeps = []

    def now(self):
        return self.now_value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_value += seconds


class Canned:
    def __init__(self, status, body=None, headers=None, text=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._body


class Slow:
    """A step that advances the fake clock, then raises (a read timeout after `seconds`)."""

    def __init__(self, seconds, error):
        self.seconds = seconds
        self.error = error


class ScriptedSession:
    """Stands in for requests.Session. Each post() takes the next step: an exception to raise, a Canned
    response, a Slow step, or nothing left = pass the call to the real inference_service app."""

    def __init__(self, steps, service=None, clock=None):
        self.steps = list(steps)
        self.client = service.test_client() if service else None
        self.clock = clock
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {}), "timeout": timeout})
        step = self.steps.pop(0) if self.steps else None
        if isinstance(step, Slow):
            self.clock.now_value += step.seconds
            raise step.error
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Canned):
            return step
        response = self.client.post(urlparse(url).path, data=data, headers=headers)
        return Canned(response.status_code, response.get_json(), dict(response.headers))


def counter(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def make_service(**config):
    return create_app({"TESTING": True, "INFERENCE_BACKEND": "mock", **config})


def make_client(steps=(), service=None, clock=None, **kwargs):
    clock = clock or Clock()
    session = ScriptedSession(steps, service=service or make_service(), clock=clock)
    client = HttpInference(BASE, session=session, clock=clock.now, sleep=clock.sleep, **kwargs)
    return client, session, clock


BUSY = Canned(503, {"error": "busy"}, {"Retry-After": "1"})
UNAVAILABLE = Canned(503, {"error": "model_unavailable"})


# --- success -----------------------------------------------------------------

def test_success_returns_the_service_result():
    client, session, _ = make_client()

    result = client.analyze(JPEG, "permitted.jpg")

    assert isinstance(result, InferenceResult)
    assert result.verdict == "permitted"
    assert len(session.calls) == 1


def test_request_shape():
    client, session, _ = make_client()
    client.analyze(JPEG, "한글 사진.jpg")

    call = session.calls[0]
    assert call["url"] == BASE + "/v1/analyze"
    assert call["data"] == JPEG  # the raw bytes, never re-encoded (ADR 0006)
    assert call["headers"]["Content-Type"] == "application/octet-stream"
    assert call["headers"]["X-Filename"] == "%ED%95%9C%EA%B8%80%20%EC%82%AC%EC%A7%84.jpg"
    assert call["headers"]["X-Request-Id"]


def test_trailing_slash_in_the_base_url_is_fine():
    session = ScriptedSession([], service=make_service())
    client = HttpInference(BASE + "/", session=session)

    client.analyze(JPEG, "a.jpg")

    assert session.calls[0]["url"] == BASE + "/v1/analyze"


def test_it_looks_like_the_other_adapters():
    client, _, _ = make_client()

    assert client.name == "http"
    assert client.state == "remote"  # readyz must not depend on the AI service (3.4)
    client.warm_up()  # nothing to load here


# --- not retried -------------------------------------------------------------

def test_invalid_image_is_not_retried():
    client, session, clock = make_client([Canned(400, {"error": "invalid_image"})])

    with pytest.raises(InvalidImageError):
        client.analyze(b"not an image", "x.jpg")

    assert len(session.calls) == 1
    assert clock.sleeps == []


def test_the_real_service_400_for_an_empty_photo_is_invalid_image_too():
    client, session, _ = make_client()

    with pytest.raises(InvalidImageError):
        client.analyze(b"", "x.jpg")

    assert len(session.calls) == 1


@pytest.mark.parametrize("response", [
    UNAVAILABLE,
    Canned(500, {"error": "internal_error"}),
    Canned(502, text="bad gateway"),
    Canned(404, {"error": "not_found"}),
    Canned(413, {"error": "too_large"}),
    Canned(405, {"error": "method_not_allowed"}),
    Canned(200, text="<html>not json</html>"),
    Canned(200, {"verdict": "violation", "window": None, "stickers": [], "elapsed_ms": 1}),  # contradicts itself
    Canned(200, ["not", "an", "object"]),
])
def test_other_failures_are_not_retried(response):
    client, session, clock = make_client([response])

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 1
    assert clock.sleeps == []


def test_a_connection_dropped_after_sending_is_not_retried():
    """Unlike "could not connect", the service may already have received the photo."""
    dropped = requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError("Connection aborted"))
    client, session, clock = make_client([dropped])

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 1
    assert clock.sleeps == []


def test_read_timeout_is_not_retried():
    client, session, _ = make_client([Slow(10, requests.exceptions.ReadTimeout())])

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 1  # the service may still be working on that photo


# --- retried -----------------------------------------------------------------

def test_busy_is_retried_once_after_the_retry_after_wait():
    client, session, clock = make_client([BUSY])

    result = client.analyze(JPEG, "permitted.jpg")

    assert result.verdict == "permitted"
    assert len(session.calls) == 2
    assert clock.sleeps == [1.0]
    assert session.calls[0]["headers"]["X-Request-Id"] == session.calls[1]["headers"]["X-Request-Id"]


@pytest.mark.parametrize("error", [requests.exceptions.ConnectionError("refused"),
                                   requests.exceptions.ConnectTimeout("no route")])
def test_connection_failure_is_retried_once(error):
    client, session, clock = make_client([error], retry_backoff=0.2)

    assert client.analyze(JPEG, "x.jpg").verdict == "violation"
    assert len(session.calls) == 2
    assert clock.sleeps == [0.2]


@pytest.mark.parametrize("retries, expected_calls", [(0, 1), (1, 2), (2, 3)])
def test_the_number_of_retries_is_exactly_what_was_configured(retries, expected_calls):
    client, session, _ = make_client([BUSY] * 10, retries=retries, deadline=60)

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == expected_calls


def test_two_connection_failures_in_a_row_fail_after_the_single_retry():
    client, session, _ = make_client([requests.exceptions.ConnectionError()] * 5)

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 2


def test_no_retry_when_the_wait_would_eat_the_whole_deadline():
    client, session, clock = make_client([BUSY], deadline=1.0)

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 1
    assert clock.sleeps == []


def test_a_retry_after_longer_than_the_cap_is_not_waited_for():
    client, session, clock = make_client([Canned(503, {"error": "busy"}, {"Retry-After": "30"})],
                                         deadline=60, max_retry_wait=2.0)

    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert len(session.calls) == 1


def test_missing_or_garbled_retry_after_falls_back_to_the_backoff():
    client, session, clock = make_client(
        [Canned(503, {"error": "busy"}), Canned(503, {"error": "busy"}, {"Retry-After": "soon"})],
        retries=2, retry_backoff=0.3, deadline=60)

    client.analyze(JPEG, "x.jpg")

    assert clock.sleeps == [0.3, 0.3]


# --- timeouts and deadline ---------------------------------------------------

def test_request_timeouts_are_connect_timeout_and_the_remaining_deadline():
    client, session, _ = make_client(deadline=10, connect_timeout=1)

    client.analyze(JPEG, "x.jpg")

    assert session.calls[0]["timeout"] == (1, 10)


def test_the_retry_gets_only_what_is_left_of_the_deadline():
    client, session, clock = make_client([BUSY], deadline=10, connect_timeout=1)

    client.analyze(JPEG, "x.jpg")

    connect, read = session.calls[1]["timeout"]
    assert connect == 1
    assert read == pytest.approx(9.0)  # 10 s deadline - 1 s Retry-After wait


def test_connect_timeout_is_never_longer_than_the_time_left():
    client, session, clock = make_client([BUSY], deadline=1.5, connect_timeout=1, retry_backoff=0.2)

    client.analyze(JPEG, "x.jpg")

    connect, read = session.calls[1]["timeout"]
    assert connect <= read == pytest.approx(0.5)


# --- bulkhead ----------------------------------------------------------------

class BlockingSession:
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0
        self._mutex = threading.Lock()

    def post(self, url, data=None, headers=None, timeout=None):
        with self._mutex:
            self.calls += 1
        assert self.release.wait(10)
        return Canned(200, {"verdict": "violation", "window": {"coords": [0, 0, 1, 1], "confidence": 0.9},
                            "stickers": [], "elapsed_ms": 1, "backend": "yolo"})


def test_bulkhead_refuses_instead_of_piling_up_api_threads():
    session = BlockingSession()
    client = HttpInference(BASE, session=session, max_concurrency=2)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.analyze(JPEG, "x.jpg"))) for _ in range(2)]
    for t in threads:
        t.start()
    wait_until = time.time() + 5
    while session.calls < 2 and time.time() < wait_until:
        time.sleep(0.005)
    before = counter("pg_ai_client_requests_total", outcome="bulkhead_full")

    with pytest.raises(InferenceError, match="too many"):
        client.analyze(JPEG, "x.jpg")  # a 3rd call while 2 are in flight

    assert session.calls == 2  # the refused photo was never sent
    assert counter("pg_ai_client_requests_total", outcome="bulkhead_full") == before + 1
    assert counter("pg_ai_client_in_flight") == 2

    session.release.set()
    for t in threads:
        t.join(10)
    assert len(results) == 2
    assert counter("pg_ai_client_in_flight") == 0
    assert client.analyze(JPEG, "x.jpg").verdict == "violation"  # slots came back


def test_bulkhead_slot_is_returned_after_a_failure():
    client, session, _ = make_client([Canned(500, {})] * 3, max_concurrency=1)

    for _ in range(3):
        with pytest.raises(InferenceError, match="status 500"):
            client.analyze(JPEG, "x.jpg")


# --- metrics -----------------------------------------------------------------

@pytest.mark.parametrize("steps, outcome, error", [
    ([], "ok", None),
    ([Canned(400, {"error": "invalid_image"})], "invalid_image", InvalidImageError),
    ([BUSY, BUSY], "busy", InferenceError),
    ([UNAVAILABLE], "unavailable", InferenceError),
    ([Slow(10, requests.exceptions.ReadTimeout())], "timeout", InferenceError),
    ([requests.exceptions.ConnectionError()] * 2, "connect_error", InferenceError),
    ([requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError("Connection aborted"))],
     "connection_lost", InferenceError),
    ([Canned(200, text="oops")], "bad_response", InferenceError),
])
def test_every_outcome_is_counted_once(steps, outcome, error):
    client, _, _ = make_client(steps)
    before = counter("pg_ai_client_requests_total", outcome=outcome)
    count_before = counter("pg_ai_client_duration_seconds_count")

    if error:
        with pytest.raises(error):
            client.analyze(JPEG, "permitted.jpg")
    else:
        client.analyze(JPEG, "permitted.jpg")

    assert counter("pg_ai_client_requests_total", outcome=outcome) == before + 1
    assert counter("pg_ai_client_duration_seconds_count") == count_before + 1


def test_retries_are_counted_by_reason():
    client, _, _ = make_client([BUSY, requests.exceptions.ConnectionError()], retries=2, deadline=60)
    busy = counter("pg_ai_client_retries_total", reason="busy")
    connect = counter("pg_ai_client_retries_total", reason="connect_error")

    client.analyze(JPEG, "x.jpg")

    assert counter("pg_ai_client_retries_total", reason="busy") == busy + 1
    assert counter("pg_ai_client_retries_total", reason="connect_error") == connect + 1


# --- Prototype mode is untouched ---------------------------------------------

def test_the_in_process_adapters_keep_their_contract():
    result = MockInference().analyze(JPEG, "permitted.jpg")

    assert result.to_dict()["verdict"] == "permitted"
    assert MockInference.state == "ready"


# --- over real sockets -------------------------------------------------------

class LiveService:
    def __init__(self, **config):
        self.app = make_service(**config)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()  # release the port, so a later connect to it is really refused
        self.thread.join(5)


def test_over_a_real_socket():
    with LiveService() as live:
        result = HttpInference(live.url).analyze(JPEG, "permitted.jpg")

    assert result.verdict == "permitted"


def test_a_closed_port_fails_fast_after_one_retry():
    with LiveService() as live:
        url = live.url
    # connect_timeout is short because Windows loopback takes ~2 s to report "refused" (measured), which would
    # otherwise dominate; either way the failure is a connect failure and must not wait for the 5 s deadline.
    client = HttpInference(url, deadline=5, connect_timeout=0.3, retry_backoff=0.05)
    retries_before = counter("pg_ai_client_retries_total", reason="connect_error")

    started = time.perf_counter()
    with pytest.raises(InferenceError):
        client.analyze(JPEG, "x.jpg")

    assert time.perf_counter() - started < 3.0
    assert counter("pg_ai_client_retries_total", reason="connect_error") == retries_before + 1  # exactly one retry


def test_a_slow_service_hits_the_deadline_once():
    with LiveService(INFERENCE_FAULT_DELAY_MS=1500) as live:
        client = HttpInference(live.url, deadline=0.3)
        started = time.perf_counter()
        with pytest.raises(InferenceError):
            client.analyze(JPEG, "x.jpg")
        elapsed = time.perf_counter() - started

    assert 0.25 <= elapsed < 1.2
