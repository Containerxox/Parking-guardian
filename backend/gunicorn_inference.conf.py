"""gunicorn settings of the inference service (`gunicorn -c gunicorn_inference.conf.py wsgi_inference:app`).

workers is 1 and on_starting refuses anything else, including `--workers N` on the command line (the command
line beats this file): AdmissionGate and the single model copy live in one process (inference_service/runtime.py).
"""
import logging
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 1
# 1 running + INFERENCE_MAX_WAITING (2) waiting + 1 for the probes. Fewer and gunicorn queues what the gate
# should refuse.
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
# Worker heartbeat timeout, not a request limit (gthread keeps beating while threads work).
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
# Seconds a worker may take to finish requests after SIGTERM. Explicit on purpose: the longest legitimate request
# is (1 running + 2 waiting) x a few seconds, far below 30, and the Pod terminationGracePeriodSeconds must be
# at least this plus the time to stop (checked with the manifests, step 5-4).
graceful_timeout = 30
accesslog = "-"

log = logging.getLogger("gunicorn.error")


def on_starting(server):
    from app.config import Config
    from inference_service.runtime import ensure_single_worker

    for warning in ensure_single_worker(server.cfg.workers, server.cfg.threads, Config.INFERENCE_MAX_WAITING):
        log.warning(warning)


def post_worker_init(worker):
    """Right after the worker imported the app: metrics server on its own port, model load in the background."""
    from inference_service.runtime import start_background

    start_background(worker.app.wsgi())
