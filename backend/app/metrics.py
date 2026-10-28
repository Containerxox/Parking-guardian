"""Prometheus metrics (prometheus-client). One module-level registry entry per metric; Flask hooks and the
inference adapter record into them. Scraped from a separate HTTP port, not through Flask: when the 4 gunicorn
threads are all stuck behind the inference lock, a /metrics route would hang too and we would lose exactly the
samples we want to see. gunicorn.conf.py starts that server (METRICS_PORT, default 9102) once per worker.

Metric names: the http_* ones follow the usual conventions; everything specific to this app is prefixed pg_.
"""
import logging
import time

from flask import g, request
from prometheus_client import Counter, Enum, Gauge, Histogram, start_http_server

log = logging.getLogger(__name__)

# --- HTTP ------------------------------------------------------------------
# Buckets reach 120 s: that is the gunicorn/ALB timeout, so a request queued behind inference until it dies
# still lands in a bucket instead of only in +Inf.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "Request latency by route template, method and status.",
    ["method", "endpoint", "status"], buckets=_HTTP_BUCKETS)
HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "http_requests_in_flight", "Requests currently inside Flask (all 4 gunicorn threads busy = 4).")

# --- Inference -------------------------------------------------------------
_INFERENCE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

INFERENCE_LOCK_WAIT = Histogram(
    "pg_inference_lock_wait_seconds",
    "Time a photo waited for the single inference lock. The direct signature of head-of-line blocking.",
    buckets=_INFERENCE_BUCKETS)
INFERENCE_STAGE = Histogram(
    "pg_inference_stage_seconds", "Time per pipeline stage (decode, window, sticker).",
    ["stage"], buckets=_INFERENCE_BUCKETS)
INFERENCE_REQUESTS = Counter(
    "pg_inference_requests_total", "Photos analyzed by verdict and outcome (ok, invalid_image, error).",
    ["verdict", "outcome"])
INFERENCE_IN_FLIGHT = Gauge(
    "pg_inference_in_flight", "Photos inside analyze(): waiting for the lock or being processed.")
MODEL_LOAD_SECONDS = Gauge("pg_model_load_seconds", "Wall time of the last successful model load.")
MODEL_STATE = Enum("pg_inference_model_state", "Model state of this process.",
                   states=["cold", "loading", "ready", "failed"])


def init_metrics(app) -> None:
    """Record duration, status and in-flight count for every request handled by this app."""

    @app.before_request
    def _start_timer():
        g._metrics_started = time.perf_counter()
        HTTP_REQUESTS_IN_FLIGHT.inc()

    @app.after_request
    def _remember_status(response):
        g._metrics_status = response.status_code
        return response

    # teardown_request also runs when a handler raised, so in-flight never drifts upward.
    @app.teardown_request
    def _observe(exc):
        started = g.pop("_metrics_started", None)
        if started is None:
            return
        HTTP_REQUESTS_IN_FLIGHT.dec()
        status = g.pop("_metrics_status", 500 if exc is not None else 0)
        endpoint = request.url_rule.rule if request.url_rule is not None else "unmatched"
        HTTP_REQUEST_DURATION.labels(request.method, endpoint, str(status)).observe(time.perf_counter() - started)


def start_metrics_server(port: int) -> None:
    """Expose /metrics on its own port in a daemon thread. A second worker in the same container would find the
    port taken; log it instead of crashing (then only the first worker is scraped -- use multiprocess mode if
    WEB_CONCURRENCY ever goes above 1)."""
    try:
        start_http_server(port)
        log.info("metrics server listening on :%d", port)
    except OSError as exc:
        log.warning("metrics server not started on :%d (%s)", port, exc)
