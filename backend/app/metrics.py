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

# Label is "route", not "endpoint": Prometheus Operator stamps every scraped series with endpoint=<port name>
# ("metrics"), and with honor_labels off the app's own endpoint label gets renamed to exported_endpoint. A name
# that nobody else uses avoids the clash (found 2026-10-28: every dashboard query on endpoint came back empty).
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "Request latency by route template, method and status.",
    ["method", "route", "status"], buckets=_HTTP_BUCKETS)
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
INFERENCE_RUNNING = Gauge(
    "pg_inference_running", "Photos inside the model right now (inference service; 0 or 1).")
INFERENCE_WAITING = Gauge(
    "pg_inference_waiting", "Photos queued behind the running one (inference service; at most INFERENCE_MAX_WAITING).")
INFERENCE_REJECTED = Counter(
    "pg_inference_rejected_total", "Photos refused with 503 busy because running and waiting were both full.")
# --- AI client (API side, HttpInference) -----------------------------------
AI_CLIENT_REQUESTS = Counter(
    "pg_ai_client_requests_total",
    "analyze() calls to the inference service by final outcome (ok, invalid_image, busy, unavailable, timeout, "
    "connect_error, connection_lost, bad_response, bulkhead_full).", ["outcome"])
AI_CLIENT_DURATION = Histogram(
    "pg_ai_client_duration_seconds", "Whole analyze() call as the API sees it, retries and waits included.",
    buckets=_INFERENCE_BUCKETS)
AI_CLIENT_RETRIES = Counter(
    "pg_ai_client_retries_total", "Extra attempts after a first failure, by reason (busy, connect_error).",
    ["reason"])
AI_CLIENT_IN_FLIGHT = Gauge(
    "pg_ai_client_in_flight", "Calls to the inference service in progress (the bulkhead counts these).")
MODEL_LOAD_SECONDS = Gauge("pg_model_load_seconds", "Wall time of the last successful model load (checkpoints, disk -> memory).")
MODEL_FIRST_INFERENCE_SECONDS = Gauge(
    "pg_model_first_inference_seconds",
    "Wall time of the first forward pass of both models after the load (worker preload). Separate from the load itself.")
MODEL_STATE = Enum("pg_inference_model_state", "Model state of this process.",
                   states=["cold", "loading", "ready", "failed"])

# --- Phase 2 pipeline (relay process, observability stage 2) ---------------------------------------------
# Exposed by `flask outbox-relay` on METRICS_PORT (PodMonitor `relay`). Labels are small fixed sets only: never a company, machine
# or job id (cardinality). A value that is NOT available is not exported at all (no series), never exported as 0: see QueueDepthPoller.
_PIPELINE_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

QUEUE_MESSAGES = Gauge(
    "pg_queue_messages",
    "Approximate messages in the SQS queue, from GetQueueAttributes (ApproximateNumberOfMessages / ...NotVisible / ...Delayed). "
    "queue = main (judgement FIFO) or dlq. SQS returns approximations. The relay drains the DLQ every pass, so a DLQ message that "
    "was already moved into the database shows up as pg_jobs{status=\"dead_lettered\"}, not here. No series = the poll failed (unknown, not 0).",
    ["queue", "state"])
QUEUE_DEPTH_LAST_SUCCESS = Gauge(
    "pg_queue_depth_last_success_timestamp_seconds", "Unix time of the last successful queue depth poll. Missing = never succeeded in this process.")
SQS_API_ERRORS = Counter(
    "pg_sqs_api_errors_total", "Failed SQS calls made by the relay, by operation (send, receive_dead_letters, get_queue_attributes).", ["operation"])
JOBS = Gauge(
    "pg_jobs", "Detection jobs in the database by status (pending_publish, queued, processing, dead_lettered). Counted by the relay every ~15 s. "
    "dead_lettered stays until the job is redriven, so it is the durable DLQ signal.", ["status"])
OLDEST_OPEN_JOB_AGE = Gauge(
    "pg_oldest_open_job_age_seconds",
    "Age of the oldest UNFINISHED job: now - min(detection_jobs.created_at) over status pending_publish, queued or processing "
    "(database clock; 0 when there is none). NOT the SQS ApproximateAgeOfOldestMessage: it also covers a job that is waiting to be "
    "published or being judged right now, and it is exact at the time of the pass instead of CloudWatch's delayed approximation.")
OLDEST_PENDING_PUBLISH_AGE = Gauge(
    "pg_oldest_pending_publish_age_seconds", "Age of the oldest job still pending_publish (database clock; 0 when none).")
RELAY_PUBLISH = Counter(
    "pg_relay_publish_total", "Jobs the relay tried to send to the queue, by outcome (success, failure). Fast-path sends by the API are not counted.",
    ["outcome"])
RELAY_PUBLISH_DELAY = Histogram(
    "pg_relay_publish_delay_seconds", "Seconds from the job's creation (ingest) to the relay's successful send. Only jobs the relay published.",
    buckets=_PIPELINE_BUCKETS)
RELAY_LAST_PASS = Gauge(
    "pg_relay_last_pass_timestamp_seconds", "Unix time the relay last finished a pass. A stale value means the loop is stuck.")
for _outcome in ("success", "failure"):        # a running relay exports 0 (nothing published / nothing failed), so rate() is a real 0, not "no data"
    RELAY_PUBLISH.labels(_outcome)
for _operation in ("send", "receive_dead_letters", "get_queue_attributes"):
    SQS_API_ERRORS.labels(_operation)


# --- Phase 2 judgement worker (`flask judge-worker`, observability stage 3) ------------------------------
# One message at a time per process, so the default registry is enough. Labels are small fixed sets: never a company, machine, job
# or event id. Model readiness is NOT duplicated here: pg_inference_model_state (above) is set by the same adapter the worker uses.
_WORKER_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 60, 120, 300, 600)
WORKER_MESSAGES = Counter(
    "pg_worker_messages_total",
    "Queue messages the worker finished handling, by outcome. done/stale = the verdict was COMMITTED to the database (counted after the commit); "
    "effects_retry = verdict committed but the evidence photo or notification failed (redelivery redoes only that part); "
    "rejected = no verdict (missing or invalid image); retry = failed before a verdict was stored (storage, inference or database "
    "error; the message stays in the queue, after maxReceiveCount it goes to the DLQ); already = redelivery of a finished job; busy = another worker "
    "holds the lease; lost_claim = lease lost, result dropped; isolated = unreadable message given back.",
    ["outcome"])
WORKER_PROCESS_SECONDS = Histogram(
    "pg_worker_process_seconds",
    "Seconds from winning the claim to the end of the handling, for claimed messages only (image read, inference, verdict transaction, "
    "effects, delete), by outcome (done, stale, rejected, retry, effects_retry, lost_claim). Inference stages are in pg_inference_stage_seconds.",
    ["outcome"], buckets=_WORKER_BUCKETS)
JOB_E2E = Histogram(
    "pg_job_e2e_seconds",
    "SERVER-side end to end seconds: from detection_jobs.received_at (API server clock when the photo was accepted) to the worker's clock right "
    "after the verdict transaction committed. Observed once per job (done and stale), never for a redelivery or a rolled-back attempt. "
    "Not the device-side time (the device clock is not trusted) and it ends at the commit, before effects and the SQS delete.",
    buckets=_WORKER_BUCKETS)
QUEUE_WAIT = Histogram(
    "pg_queue_wait_seconds",
    "Seconds from the SQS SentTimestamp of a message to the worker receiving it, first delivery only (ApproximateReceiveCount = 1). AWS and pod "
    "clocks differ by a little, so values are approximate (SQS is second-precise at best). Includes time spent behind the machine's earlier message "
    "in the FIFO group. Excludes the wait before the first send (Relay-published jobs are in pg_relay_publish_delay_seconds).",
    buckets=_WORKER_BUCKETS)
WORKER_TIMING_SKIPPED = Counter(
    "pg_worker_timing_skipped_total",
    "Timing observations not recorded, by metric (e2e, queue_wait) and reason (missing = timestamp unknown, negative = clock skew made it < 0).",
    ["metric", "reason"])
for _outcome in ("done", "stale", "effects_retry", "rejected", "retry", "already", "busy", "lost_claim", "isolated"):
    WORKER_MESSAGES.labels(_outcome)     # a running worker exports 0 so rate() is a real 0, not "no data"
for _metric in ("e2e", "queue_wait"):
    for _reason in ("missing", "negative"):
        WORKER_TIMING_SKIPPED.labels(_metric, _reason)


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
        route = request.url_rule.rule if request.url_rule is not None else "unmatched"
        HTTP_REQUEST_DURATION.labels(request.method, route, str(status)).observe(time.perf_counter() - started)


def start_metrics_server(port: int) -> None:
    """Expose /metrics on its own port in a daemon thread. A second worker in the same container would find the
    port taken; log it instead of crashing (then only the first worker is scraped -- use multiprocess mode if
    WEB_CONCURRENCY ever goes above 1)."""
    try:
        start_http_server(port)
        log.info("metrics server listening on :%d", port)
    except OSError as exc:
        log.warning("metrics server not started on :%d (%s)", port, exc)
