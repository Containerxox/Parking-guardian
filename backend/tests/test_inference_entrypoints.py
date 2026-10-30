"""How the two services are started (design doc 9.6 step 2-6).

gunicorn itself cannot run on Windows (it imports `grp`), so these tests load the config files as plain Python
modules and call their hooks with stand-ins for gunicorn's server and worker objects. The real gunicorn run
is checked separately (docs/measurements/phase1/local/).

What is pinned here:
  * the inference service always runs ONE worker: AdmissionGate counts inside one process, so a second worker
    would silently double the admitted photos and the model memory (design doc 3.5);
  * the API reports how many AI calls can really be in flight, using the worker/thread numbers gunicorn
    actually has, not the ones the settings assume;
  * the Prototype start-up (models load in the background) is untouched;
  * torch threads: the service defaults to 1, the Prototype keeps torch's own default.
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app
from app.adapters.inference import HttpInference, YoloInference
from inference_service.runtime import ensure_single_worker, start_background

BACKEND = Path(__file__).resolve().parent.parent


def load_module(filename, name):
    """Import a file such as gunicorn_inference.conf.py (the dots in its name rule out a normal import)."""
    spec = importlib.util.spec_from_file_location(name, BACKEND / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    spec.loader.exec_module(module)
    return module


class Recorder:
    state = "cold"
    name = "recorder"

    def __init__(self):
        self.warm_ups = 0

    def warm_up(self):
        self.warm_ups += 1


def fake_worker(app, workers=1, threads=4):
    return SimpleNamespace(app=SimpleNamespace(wsgi=lambda: app), cfg=SimpleNamespace(workers=workers, threads=threads))


def fake_server(workers=1, threads=4):
    return SimpleNamespace(cfg=SimpleNamespace(workers=workers, threads=threads))


# --- inference service: one worker -------------------------------------------

def test_one_worker_is_accepted():
    assert ensure_single_worker(workers=1, threads=4, max_waiting=2) == []


@pytest.mark.parametrize("workers", [0, 2, 4])
def test_more_than_one_worker_is_refused_with_the_reason(workers):
    with pytest.raises(RuntimeError, match="AdmissionGate"):
        ensure_single_worker(workers=workers, threads=4, max_waiting=2)


def test_too_few_threads_for_the_waiting_line_is_reported():
    """1 running + max_waiting waiting + 1 for the probes: fewer threads and gunicorn queues requests the gate
    would have refused, and the health checks can starve."""
    warnings = ensure_single_worker(workers=1, threads=3, max_waiting=2)

    assert len(warnings) == 1 and "threads" in warnings[0]
    assert ensure_single_worker(workers=1, threads=4, max_waiting=2) == []
    assert ensure_single_worker(workers=1, threads=2, max_waiting=0) == []


def test_inference_config_file_fixes_one_worker_and_explicit_timeouts():
    conf = load_module("gunicorn_inference.conf.py", "gunicorn_inference_conf")

    assert conf.workers == 1
    assert conf.threads == 4  # 1 running + 2 waiting + 1 for the probes
    assert conf.graceful_timeout == 30  # explicit: the longest legitimate request is a few seconds
    assert conf.bind.endswith(":8000")


def test_start_is_refused_when_the_command_line_asks_for_more_workers():
    """`gunicorn --workers 3` overrides the config file, so the hook looks at what gunicorn really has."""
    conf = load_module("gunicorn_inference.conf.py", "gunicorn_inference_conf")

    conf.on_starting(fake_server(workers=1, threads=4))
    with pytest.raises(RuntimeError, match="one worker"):
        conf.on_starting(fake_server(workers=3, threads=4))


def test_worker_hook_warms_up_the_model_once():
    conf = load_module("gunicorn_inference.conf.py", "gunicorn_inference_conf")
    app = create_app_for_hooks()

    conf.post_worker_init(fake_worker(app))

    assert app.extensions["pg_inference"].warm_ups == 1


def test_start_background_skips_the_metrics_server_when_the_port_is_zero():
    service = SimpleNamespace(config={"METRICS_PORT": 0}, extensions={"pg_inference": Recorder()})

    start_background(service)

    assert service.extensions["pg_inference"].warm_ups == 1


def test_wsgi_inference_exposes_an_app_without_loading_a_model():
    wsgi = load_module("wsgi_inference.py", "wsgi_inference_module")

    assert wsgi.app.test_client().get("/healthz").status_code == 200
    assert wsgi.app.extensions["pg_inference"].state in ("cold", "ready")  # building the app loads nothing


# --- API: Prototype start-up unchanged, http start-up reports the bulkhead --

def create_app_for_hooks(**config):
    app = create_app({"TESTING": True, "INFERENCE_BACKEND": "mock", "METRICS_PORT": 0, **config})
    app.extensions["pg_inference"] = Recorder()
    return app


def test_api_hook_still_warms_up_a_local_model():
    conf = load_module("gunicorn.conf.py", "gunicorn_api_conf")
    app = create_app_for_hooks()

    conf.post_worker_init(fake_worker(app))

    assert app.extensions["pg_inference"].warm_ups == 1  # Prototype behaviour


def test_api_hook_does_not_warm_up_when_the_model_is_remote(caplog):
    conf = load_module("gunicorn.conf.py", "gunicorn_api_conf")
    app = create_app_for_hooks()
    app.extensions["pg_inference"] = HttpInference("http://inference:8000", max_concurrency=2, session=object())

    with caplog.at_level(logging.INFO):
        conf.post_worker_init(fake_worker(app, workers=1, threads=8))

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "AI_CLIENT_MAX_CONCURRENCY=2" in messages and "8 threads" in messages
    assert not any(r.levelno >= logging.WARNING and "AI_CLIENT_MAX_CONCURRENCY" in r.getMessage()
                   for r in caplog.records)


def test_api_hook_warns_with_the_real_thread_count(caplog):
    """The settings may say 8 threads while the command line gave 4; the hook reports what gunicorn has."""
    conf = load_module("gunicorn.conf.py", "gunicorn_api_conf")
    app = create_app_for_hooks()
    app.extensions["pg_inference"] = HttpInference("http://inference:8000", max_concurrency=4, session=object())

    with caplog.at_level(logging.WARNING):
        conf.post_worker_init(fake_worker(app, workers=1, threads=4))

    assert any("4 threads" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_api_hook_counts_the_limit_per_worker(caplog):
    conf = load_module("gunicorn.conf.py", "gunicorn_api_conf")
    app = create_app_for_hooks()
    app.extensions["pg_inference"] = HttpInference("http://inference:8000", max_concurrency=2, session=object())

    with caplog.at_level(logging.INFO):
        conf.post_worker_init(fake_worker(app, workers=2, threads=8))

    assert "4 in total" in " ".join(r.getMessage() for r in caplog.records)  # each worker has its own bulkhead


# --- torch threads ------------------------------------------------------------

class FakeTorch:
    def __init__(self):
        self.calls = []

    def set_num_threads(self, n):
        self.calls.append(n)


def test_torch_threads_are_applied_when_set():
    model = YoloInference("yolov5", "w.pt", "s.pt", torch_threads=1)
    torch = FakeTorch()

    model._apply_torch_threads(torch)

    assert torch.calls == [1]


def test_prototype_leaves_torch_threads_alone_by_default():
    model = YoloInference("yolov5", "w.pt", "s.pt")
    torch = FakeTorch()

    model._apply_torch_threads(torch)

    assert torch.calls == []


def test_config_default_keeps_the_prototype_untouched():
    from app.config import Config

    assert Config.TORCH_NUM_THREADS == 0  # 0 = do not touch torch's default


def test_wsgi_inference_sets_the_openmp_default_before_torch_is_imported(monkeypatch):
    """torch.set_num_threads only reaches threads that start after it (measured); the environment variable is
    the default every thread of the pool starts with."""
    env = {k: v for k, v in os.environ.items() if k not in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "TORCH_NUM_THREADS")}
    monkeypatch.setattr(os, "environ", env)

    load_module("wsgi_inference.py", "wsgi_inference_threads")

    assert env["OMP_NUM_THREADS"] == "1" and env["MKL_NUM_THREADS"] == "1"


def test_an_explicit_torch_thread_setting_reaches_the_openmp_default(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k not in ("OMP_NUM_THREADS", "MKL_NUM_THREADS")}
    env["TORCH_NUM_THREADS"] = "2"
    monkeypatch.setattr(os, "environ", env)

    load_module("wsgi_inference.py", "wsgi_inference_threads")

    assert env["OMP_NUM_THREADS"] == "2"


def test_inference_service_defaults_to_one_torch_thread(monkeypatch):
    import inference_service

    seen = {}
    monkeypatch.setattr(inference_service, "build_inference", lambda config: seen.update(config=dict(config)) or Recorder())

    inference_service.create_app({"TESTING": True, "INFERENCE_BACKEND": "yolo"})
    assert seen["config"]["TORCH_NUM_THREADS"] == 1

    inference_service.create_app({"TESTING": True, "INFERENCE_BACKEND": "yolo", "TORCH_NUM_THREADS": 2})
    assert seen["config"]["TORCH_NUM_THREADS"] == 2
