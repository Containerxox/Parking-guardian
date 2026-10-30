"""INFERENCE_BACKEND=http: settings, build_inference, /readyz, and what the AI service being away does to the API.

The point of the split is fault isolation (ADR 0018): when the inference service is down or busy, detections
fail with the existing 503 inference_unavailable, while logins, lists and /readyz keep working. The Prototype
backends (yolo, mock) keep their behaviour; the last tests pin that.
"""
import logging
import threading
import time

import pytest
import requests

from app.adapters.inference import HttpInference, MockInference, YoloInference, build_inference
from app.config import Config
from inference_service import create_app as create_inference_app

from .conftest import API, JPEG_BYTES
from .test_inference_http import BlockingSession, Canned, Clock, ScriptedSession

AI_URL = "http://inference:8000"


def http_config(**overrides):
    config = {"INFERENCE_BACKEND": "http", "INFERENCE_URL": AI_URL, "AI_CLIENT_DEADLINE": 10.0,
              "AI_CLIENT_CONNECT_TIMEOUT": 1.0, "AI_CLIENT_RETRIES": 1, "AI_CLIENT_MAX_CONCURRENCY": 4,
              "GUNICORN_THREADS": 8}
    config.update(overrides)
    return config


@pytest.fixture
def ai_down(app):
    """The API wired to an inference service nobody answers (connection refused every time)."""
    clock = Clock()
    session = ScriptedSession([requests.exceptions.ConnectionError("refused")] * 20, clock=clock)
    switch_to_http(app, session, clock)
    return session


@pytest.fixture
def ai_up(app):
    """The API wired to a healthy inference service (the real inference_service app, mock model)."""
    clock = Clock()
    session = ScriptedSession([], service=create_inference_app({"TESTING": True, "INFERENCE_BACKEND": "mock"}),
                              clock=clock)
    switch_to_http(app, session, clock)
    return session


def switch_to_http(app, session, clock, **config):
    app.config.update(**http_config(**config))
    client = build_inference(app.config)
    client._session, client._clock, client._sleep = session, clock.now, clock.sleep
    app.extensions["pg_inference"] = client
    return client


# --- settings and build_inference -------------------------------------------

def test_config_defaults():
    assert Config.INFERENCE_URL == ""
    assert Config.AI_CLIENT_DEADLINE == 10.0
    assert Config.AI_CLIENT_CONNECT_TIMEOUT == 1.0
    assert Config.AI_CLIENT_RETRIES == 1
    assert Config.AI_CLIENT_MAX_CONCURRENCY == 4  # a starting point, not a decided value (see test below)
    assert Config.INFERENCE_BACKEND == "yolo"  # the Prototype default is untouched


def test_http_backend_is_built_from_the_settings():
    client = build_inference(http_config(INFERENCE_URL="http://ai.local:9000/", AI_CLIENT_DEADLINE=7,
                                         AI_CLIENT_CONNECT_TIMEOUT=0.5, AI_CLIENT_RETRIES=2,
                                         AI_CLIENT_MAX_CONCURRENCY=3))

    assert isinstance(client, HttpInference)
    assert client.url == "http://ai.local:9000/v1/analyze"
    assert (client.deadline, client.connect_timeout, client.retries) == (7.0, 0.5, 2)
    assert client.max_concurrency == 3


@pytest.mark.parametrize("url", ["", None])
def test_http_backend_without_a_url_refuses_to_start(url):
    with pytest.raises(ValueError, match="INFERENCE_URL"):
        build_inference(http_config(INFERENCE_URL=url))


def test_prototype_backends_are_still_what_they_were():
    assert isinstance(build_inference({"INFERENCE_BACKEND": "mock", "MOCK_INFERENCE_VERDICT": "violation"}),
                      MockInference)
    yolo = build_inference({"INFERENCE_BACKEND": "yolo", "YOLOV5_DIR": "yolov5", "WINDOW_MODEL_PATH": "w.pt",
                            "STICKER_MODEL_PATH": "s.pt", "WINDOW_CLASS_NAME": "car_window", "WINDOW_CONF": 0.25,
                            "STICKER_CONF": 0.5, "INFERENCE_IMAGE_SIZE": 640})
    assert isinstance(yolo, YoloInference)
    assert yolo.state == "cold"  # nothing is loaded just by building it


@pytest.mark.parametrize("limit, threads, warns", [(4, 4, True), (8, 8, True), (4, 8, False), (2, 4, False)])
def test_a_bulkhead_that_allows_every_api_thread_is_warned_about(caplog, limit, threads, warns):
    """The default 4 is not an operating value: with 4 API threads it would let all of them wait for the AI.
    The real value is chosen from E0/S0 (design doc 4.2, T2); until then this only warns."""
    with caplog.at_level(logging.WARNING, logger="app.adapters.inference"):
        build_inference(http_config(AI_CLIENT_MAX_CONCURRENCY=limit, GUNICORN_THREADS=threads))

    assert any("AI_CLIENT_MAX_CONCURRENCY" in record.message for record in caplog.records) is warns


# --- /readyz does not depend on the AI service ------------------------------

def test_readyz_is_200_when_the_ai_service_is_unreachable(app, client, ai_down):
    response = client.get(f"{API}/readyz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok", "inference": "remote"}
    assert ai_down.calls == []  # readiness never calls the AI service


def test_readyz_still_reports_a_broken_database(app, client, ai_up, monkeypatch):
    from sqlalchemy import text  # noqa: F401
    from app.extensions import db

    def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.session, "execute", broken)

    response = client.get(f"{API}/readyz")

    assert response.status_code == 503
    assert response.get_json()["database"] == "error"
    assert response.get_json()["inference"] == "remote"


def test_warm_up_hook_does_nothing_for_http(app, ai_down):
    from app.adapters import warm_up_inference

    warm_up_inference(app)

    assert ai_down.calls == []
    assert app.extensions["pg_inference"].state == "remote"


# --- the API keeps working while the AI service is away ---------------------

def test_admin_api_works_while_detections_fail(client, auth, upload, ai_down):
    assert client.get(f"{API}/readyz").status_code == 200
    headers = auth("kim@a.com")
    assert client.get(f"{API}/violations", headers=headers).status_code == 200
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 200

    response = upload("RPI-A-GANGNAM-01", "violation.jpg")

    assert response.status_code == 503  # the same contract the devices already know
    assert response.get_json()["error"]["code"] == "inference_unavailable"
    assert len(ai_down.calls) == 2  # the connection failure was retried once, then given up


def test_a_busy_ai_service_is_a_503_not_a_wrong_verdict(client, upload, app):
    clock = Clock()
    busy = Canned(503, {"error": "busy"}, {"Retry-After": "1"})
    switch_to_http(app, ScriptedSession([busy, busy], clock=clock), clock)

    response = upload("RPI-A-GANGNAM-01", "violation.jpg")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "inference_unavailable"


def test_an_unreadable_photo_stays_a_400(client, upload, app):
    clock = Clock()
    session = ScriptedSession([Canned(400, {"error": "invalid_image"})], clock=clock)
    switch_to_http(app, session, clock)

    response = upload("RPI-A-GANGNAM-01", "violation.jpg")

    assert response.status_code == 400
    assert len(session.calls) == 1


# --- detections through the AI service give the Prototype's answers ---------

@pytest.mark.parametrize("filename", ["violation.jpg", "permitted.jpg", "novehicle.jpg"])
def test_the_judgement_is_the_same_as_with_the_in_process_model(app, client, upload, filename):
    """What the device gets back in `inference` (verdict, window, stickers) is identical either way."""
    in_process = upload("RPI-A-GANGNAM-01", filename)
    assert in_process.status_code in (200, 201)

    session = ScriptedSession([], service=create_inference_app({"TESTING": True, "INFERENCE_BACKEND": "mock"}))
    switch_to_http(app, session, Clock())
    over_http = upload("RPI-A-GANGNAM-01", filename)

    assert over_http.status_code in (200, 201)
    assert over_http.get_json()["inference"] == in_process.get_json()["inference"]
    assert len(session.calls) == 1


def test_a_violation_is_created_through_the_ai_service(client, auth, upload, ai_up):
    response = upload("RPI-A-GANGNAM-01", "violation.jpg")

    assert response.status_code == 201
    violation_id = response.get_json()["violation_id"]
    detail = client.get(f"{API}/violations/{violation_id}", headers=auth("kim@a.com")).get_json()
    assert detail["status"] == "NOTIFIED"


def test_a_permitted_car_stores_nothing_through_the_ai_service(client, auth, upload, ai_up):
    response = upload("RPI-A-GANGNAM-01", "permitted.jpg")

    assert response.status_code == 200
    assert "violation_id" not in response.get_json()


# --- the bulkhead really protects the API threads ---------------------------

def test_with_the_ai_service_stuck_other_requests_are_not_starved(app, client, auth, upload):
    """Two photos wait inside the AI client (bulkhead 2). A third detection is refused at once, and the admin
    API answers while the first two are still stuck. A larger limit than the thread count would not."""
    headers = auth("kim@a.com")  # log in before the threads are busy
    session = BlockingSession()
    app_client = switch_to_http(app, session, Clock(), AI_CLIENT_MAX_CONCURRENCY=2)
    app_client._clock, app_client._sleep = time.monotonic, time.sleep  # real time while threads block

    stuck = [threading.Thread(target=lambda: upload("RPI-A-GANGNAM-01", "violation.jpg")) for _ in range(2)]
    for t in stuck:
        t.start()
    deadline = time.time() + 5
    while session.calls < 2 and time.time() < deadline:
        time.sleep(0.005)
    assert session.calls == 2

    started = time.perf_counter()
    refused = upload("RPI-A-GANGNAM-01", "violation.jpg")
    admin = client.get(f"{API}/violations", headers=headers)
    elapsed = time.perf_counter() - started

    assert refused.status_code == 503
    assert refused.get_json()["error"]["code"] == "inference_unavailable"
    assert admin.status_code == 200
    assert session.calls == 2  # the third photo never went out
    assert elapsed < 2.0  # nobody waited for the stuck AI calls

    session.release.set()
    for t in stuck:
        t.join(10)
