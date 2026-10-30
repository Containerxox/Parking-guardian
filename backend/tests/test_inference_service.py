"""The inference service app (inference_service/): POST /v1/analyze, /healthz, /readyz.

Contract: docs/design/ARCHITECTURE_EVOLUTION.md 3.3. The service holds no database and no secrets, so these
tests use an injected stand-in for the model and never create an `app` with a database.
"""
import pytest

from app.adapters.inference import InferenceError, InferenceResult, InvalidImageError, MockInference
from inference_service import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-content"


class StubInference:
    """Stand-in model with a settable state and an optional exception."""
    name = "stub"

    def __init__(self, state="ready", error=None):
        self.state = state
        self.error = error
        self.calls = []

    def warm_up(self):
        pass

    def analyze(self, image_bytes, filename=""):
        self.calls.append((image_bytes, filename))
        if self.error:
            raise self.error
        return InferenceResult(has_window=True, window={"coords": [0, 0, 1, 1], "confidence": 0.9},
                               elapsed_ms=5, backend=self.name)


def make_client(inference=None):
    service = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock"}, inference=inference)
    return service.test_client()


@pytest.fixture
def client():
    return make_client(MockInference())


def analyze(client, body=JPEG, filename="photo.jpg", **headers):
    if filename is not None:
        headers["X-Filename"] = filename
    return client.post("/v1/analyze", data=body, content_type="application/octet-stream", headers=headers)


# --- /v1/analyze: 200 --------------------------------------------------------

@pytest.mark.parametrize("filename, verdict, stickers", [
    ("violation.jpg", "violation", 0),
    ("permitted.jpg", "permitted", 1),
    ("novehicle.jpg", "no_vehicle", 0),
])
def test_analyze_returns_the_result_dict(client, filename, verdict, stickers):
    response = analyze(client, filename=filename)

    assert response.status_code == 200
    body = response.get_json()
    assert body["verdict"] == verdict
    assert body["sticker_count"] == stickers
    assert body["backend"] == "mock"


def test_response_is_parsed_back_by_the_api_side(client):
    """What the service sends is exactly what HttpInference will parse."""
    body = analyze(client, filename="permitted.jpg").get_json()

    restored = InferenceResult.from_dict(body)

    assert restored.verdict == "permitted"
    assert restored.to_dict() == body


def test_raw_bytes_and_filename_reach_the_model():
    stub = StubInference()
    analyze(make_client(stub), body=JPEG, filename="a.jpg")

    assert stub.calls == [(JPEG, "a.jpg")]


def test_filename_header_is_optional():
    stub = StubInference()
    response = analyze(make_client(stub), filename=None)

    assert response.status_code == 200
    assert stub.calls == [(JPEG, "")]


def test_percent_encoded_filename_is_decoded():
    """HTTP headers are ASCII, so the client percent-encodes the file name (HttpInference)."""
    from urllib.parse import quote
    stub = StubInference()
    analyze(make_client(stub), filename=quote("한글 사진.jpg", safe=""))

    assert stub.calls == [(JPEG, "한글 사진.jpg")]


def test_request_id_is_echoed(client):
    assert analyze(client, **{"X-Request-Id": "req-123"}).headers["X-Request-Id"] == "req-123"


# --- /v1/analyze: 400 --------------------------------------------------------

def test_empty_body_is_invalid_image(client):
    response = analyze(client, body=b"")

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_image"}


def test_undecodable_image_is_invalid_image():
    response = analyze(make_client(StubInference(error=InvalidImageError("cannot decode image"))))

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_image"}


# --- /v1/analyze: 503 --------------------------------------------------------

def test_model_still_loading_is_503_without_running_it():
    stub = StubInference(state="loading")
    response = analyze(make_client(stub))

    assert response.status_code == 503
    assert response.get_json() == {"error": "model_unavailable"}
    assert stub.calls == []


def test_model_failure_is_503_model_unavailable():
    response = analyze(make_client(StubInference(error=InferenceError("model load failed"))))

    assert response.status_code == 503
    assert response.get_json() == {"error": "model_unavailable"}


def test_failed_state_is_refused_without_trying_to_load_per_request():
    """A failed load is retried by the bounded background warm-up, not by every incoming photo."""
    stub = StubInference(state="failed")
    response = analyze(make_client(stub))

    assert response.status_code == 503
    assert response.get_json() == {"error": "model_unavailable"}
    assert stub.calls == []


def test_unexpected_exception_is_not_a_traceback_to_the_caller():
    response = analyze(make_client(StubInference(error=RuntimeError("boom"))))

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal_error"}


def test_analyze_only_accepts_post(client):
    assert client.get("/v1/analyze").status_code == 405


# --- health ------------------------------------------------------------------

def test_healthz_is_ok_even_while_the_model_is_loading():
    response = make_client(StubInference(state="loading")).get("/healthz")

    assert response.status_code == 200


def test_healthz_turns_red_once_loading_has_been_given_up():
    """Liveness probe fails -> kubelet restarts the container; this is the recovery path for a dead model."""
    stub = StubInference(state="failed")
    stub.gave_up = True

    response = make_client(stub).get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["status"] == "model_failed"


def test_healthz_stays_ok_while_retries_are_still_pending():
    stub = StubInference(state="failed")
    stub.gave_up = False

    assert make_client(stub).get("/healthz").status_code == 200


@pytest.mark.parametrize("state, status", [("ready", 200), ("loading", 503), ("failed", 503), ("cold", 503)])
def test_readyz_is_200_only_when_the_model_is_ready(state, status):
    response = make_client(StubInference(state=state)).get("/readyz")

    assert response.status_code == status
    assert response.get_json()["inference"] == state


def test_service_has_no_database():
    service = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock"})

    assert "sqlalchemy" not in service.extensions
    assert "pg_storage" not in service.extensions
