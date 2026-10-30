"""Start-up helpers shared by gunicorn_inference.conf.py and `python wsgi_inference.py`."""
import logging
import os

log = logging.getLogger(__name__)


def apply_thread_env(torch_threads: int) -> None:
    """Make `torch_threads` the OpenMP/MKL default of this process. Call before torch is imported.

    torch.set_num_threads() alone is not enough: it was measured to apply to threads that start after the call,
    while a thread that already ran torch keeps its old count, and gunicorn serves requests from a thread pool.
    The environment variable is what every pool thread starts from. An explicit OMP_NUM_THREADS wins.
    """
    os.environ.setdefault("OMP_NUM_THREADS", str(torch_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(torch_threads))


def ensure_single_worker(workers: int, threads: int, max_waiting: int) -> list[str]:
    """Refuse to start with more than one gunicorn worker; return warnings for a thread count that is too low.

    AdmissionGate (admission.py) counts inside one process. Two workers would each admit one running and
    max_waiting waiting photo and each hold a copy of the models, so the limit and the memory would double
    without anyone deciding it. More capacity comes from more Pods, not workers (design doc 3.5).
    """
    if workers != 1:
        raise RuntimeError(
            f"the inference service must run exactly one worker (got {workers}): AdmissionGate limits photos "
            "per process, and every worker would load its own copy of the models")
    needed = max_waiting + 2  # 1 running + max_waiting waiting + 1 for the probes
    if threads < needed:
        return [f"{threads} gunicorn threads is fewer than 1 running + {max_waiting} waiting + 1 for the probes "
                f"({needed}): gunicorn would queue requests the gate should refuse, and probes can starve"]
    return []


def start_background(service) -> None:
    """Per-worker start-up work: the metrics server on its own port, and the model load in the background
    (/readyz stays 503 until it is done). The CLI never gets here."""
    from app.adapters import warm_up_inference
    from app.metrics import start_metrics_server

    port = service.config.get("METRICS_PORT", 0)
    if port:
        start_metrics_server(port)
    warm_up_inference(service)
