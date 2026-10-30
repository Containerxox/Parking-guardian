"""Model loading in the background (YoloInference.warm_up): bounded retries, then give up.

A Pod whose load failed is NotReady, so the Service sends it no photo; "retry on the next photo" can never
happen there. A few spaced attempts cover a transient failure (model download not finished, memory spike);
after the last one `gave_up` turns /healthz red so Kubernetes restarts the container, whose own backoff
(CrashLoopBackOff) slows any further retry. Nothing here loops forever.
"""
import time

from app.adapters.inference import InferenceError, YoloInference


class FlakyYolo(YoloInference):
    """Fails the first `failures` loads without touching torch."""

    def __init__(self, failures, **kwargs):
        super().__init__("yolov5", "window.pt", "sticker.pt", **kwargs)
        self.failures = failures
        self.attempts = 0

    def _ensure_loaded(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            self._set_state("failed")
            raise InferenceError("transient failure")
        self._set_state("ready")


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_a_transient_failure_is_retried_until_ready():
    model = FlakyYolo(failures=2, load_attempts=3, retry_delay=0)

    model.warm_up()

    assert wait_for(lambda: model.state == "ready")
    assert model.attempts == 3
    assert model.gave_up is False


def test_retries_stop_after_the_configured_attempts():
    model = FlakyYolo(failures=100, load_attempts=3, retry_delay=0)

    model.warm_up()

    assert wait_for(lambda: model.gave_up)
    time.sleep(0.2)  # a runaway loop would keep counting
    assert model.attempts == 3
    assert model.state == "failed"


def test_first_success_needs_no_retry():
    model = FlakyYolo(failures=0, load_attempts=3, retry_delay=0)

    model.warm_up()

    assert wait_for(lambda: model.state == "ready")
    assert model.attempts == 1
