"""Prometheus metrics: HTTP histogram labels, in-flight gauge and the inference counters (mock backend)."""
from prometheus_client import REGISTRY

from .conftest import API


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def test_http_requests_are_recorded_by_route_template_and_status(client):
    before = _value("http_request_duration_seconds_count", method="GET", route="/api/v1/healthz", status="200") or 0
    client.get(f"{API}/healthz")
    after = _value("http_request_duration_seconds_count", method="GET", route="/api/v1/healthz", status="200")
    assert after == before + 1


def test_unknown_routes_are_labelled_unmatched(client):
    before = _value("http_request_duration_seconds_count", method="GET", route="unmatched", status="404") or 0
    client.get(f"{API}/no-such-route")
    assert _value("http_request_duration_seconds_count", method="GET", route="unmatched", status="404") == before + 1


def test_in_flight_gauge_returns_to_zero(client):
    client.get(f"{API}/healthz")
    client.get(f"{API}/no-such-route")
    assert _value("http_requests_in_flight") == 0


def test_mock_inference_does_not_touch_inference_metrics(upload):
    """MockInference has no lock and no stages; only YoloInference records pg_inference_*."""
    before = _value("pg_inference_requests_total", verdict="violation", outcome="ok") or 0
    response = upload("RPI-A-GANGNAM-01", "violation.jpg")
    assert response.status_code in (200, 201)
    assert (_value("pg_inference_requests_total", verdict="violation", outcome="ok") or 0) == before


def test_yolo_inference_records_invalid_image_and_in_flight():
    from app.adapters.inference import InvalidImageError, YoloInference

    before = _value("pg_inference_requests_total", verdict="", outcome="invalid_image") or 0
    yolo = YoloInference("/nonexistent", "/no/default.pt", "/no/last.pt")
    try:
        yolo.analyze(b"not an image")
    except InvalidImageError:
        pass
    assert _value("pg_inference_requests_total", verdict="", outcome="invalid_image") == before + 1
    assert _value("pg_inference_in_flight") == 0
    assert _value("pg_inference_stage_seconds_count", stage="decode") >= 1
