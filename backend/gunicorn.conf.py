"""gunicorn settings for the container (Dockerfile: `gunicorn -c gunicorn.conf.py wsgi:app`).

Bind, workers, threads and timeout stay on the command line / environment so docker-compose and the
Kubernetes manifests keep working unchanged. This file only adds the worker hook.
"""


def post_worker_init(worker):
    """Runs in each worker right after it imported the app. Starts loading the YOLO models in the
    background so /readyz turns 200 only when the Pod can really answer a photo (see
    YoloInference.warm_up). The CLI (`flask db upgrade`, `flask seed`) never goes through gunicorn,
    so it never loads the models."""
    from app.adapters import warm_up_inference
    from app.adapters.inference import report_bulkhead
    from app.metrics import start_metrics_server

    app = worker.app.wsgi()
    port = app.config.get("METRICS_PORT", 0)
    if port:
        start_metrics_server(port)  # /metrics on its own port, independent of the 4 request threads
    inference = app.extensions["pg_inference"]
    if getattr(inference, "state", None) == "remote":
        # INFERENCE_BACKEND=http: nothing to load here. Report the real worker/thread numbers gunicorn has.
        report_bulkhead(inference, threads=worker.cfg.threads, workers=worker.cfg.workers)
    else:
        warm_up_inference(app)  # Prototype: load the models in the background
