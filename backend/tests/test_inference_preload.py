"""YoloInference.preload(): load the models and run both of them once, synchronously, before anything is consumed (P5, ADR 0019).

Why a new method: warm_up() (gunicorn) loads in a background thread and returns at once, so the API can answer /healthz
while /readyz stays 503. A queue worker has no such readiness gate: it must not call receive() until the models are in
memory and have run once, otherwise the first message pays the load (25-35 s locally) inside its 55-second lease.

Two costs are measured separately, because they behave differently on a fresh Pod:
  load_seconds             torch.hub.load of the two checkpoints (disk -> memory)
  first_inference_seconds  the first forward pass of both models (allocator, kernel and thread-pool start-up)
  second_inference_seconds the same forward pass again = the steady cost
No torch or checkpoint is touched here: the load and the forward pass are replaced by fakes.
"""
import pytest

from app import metrics
from app.adapters.inference import InferenceError, MockInference, YoloInference


class FakeClock:
    """perf_counter replacement: every call returns the next value, so durations are exact."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class FakeYolo(YoloInference):
    def __init__(self, load_failures=0, forward_failures=0, **kwargs):
        super().__init__("yolov5", "window.pt", "sticker.pt", **kwargs)
        self.load_failures, self.forward_failures = load_failures, forward_failures
        self.loads = self.forwards = 0
        self.events = []

    def _ensure_loaded(self):
        if self._window_model is not None:
            return
        self.loads += 1
        self.events.append("load")
        if self.loads <= self.load_failures:
            self._set_state("failed")
            raise InferenceError("transient failure")
        self._window_model = self._sticker_model = object()
        self._set_state("ready")

    def _warm_forward(self):
        self.forwards += 1
        self.events.append("forward")
        if self.forwards <= self.forward_failures:
            raise RuntimeError("kernel launch failed")


def test_preload_loads_then_runs_the_models_twice_and_reports_each_cost_separately():
    model = FakeYolo()
    report = model.preload(clock=FakeClock(0.0, 12.5, 12.5, 14.0, 14.0, 14.4))
    assert model.events == ["load", "forward", "forward"]
    assert report == {"attempts": 1, "load_seconds": 12.5, "first_inference_seconds": 1.5, "second_inference_seconds": 0.4}
    assert model.state == "ready"


def test_the_first_inference_cost_is_published_next_to_the_load_cost():
    model = FakeYolo()
    model.preload(clock=FakeClock(0.0, 10.0, 10.0, 12.0, 12.0, 12.5))
    assert metrics.MODEL_FIRST_INFERENCE_SECONDS._value.get() == 2.0
    assert metrics.MODEL_LOAD_SECONDS is not None   # the existing gauge keeps meaning "disk -> memory"


def test_a_transient_load_failure_is_retried_synchronously_before_returning():
    sleeps = []
    model = FakeYolo(load_failures=2, load_attempts=3, retry_delay=5)
    report = model.preload(clock=FakeClock(*([0.0] * 20)), sleep=sleeps.append)
    assert model.loads == 3 and report["attempts"] == 3
    assert sleeps == [5, 10]            # retry_delay * attempt, as warm_up() does
    assert model.state == "ready" and model.gave_up is False


def test_giving_up_raises_instead_of_returning_a_half_loaded_model():
    model = FakeYolo(load_failures=100, load_attempts=3, retry_delay=0)
    with pytest.raises(InferenceError):
        model.preload(sleep=lambda s: None)
    assert model.loads == 3 and model.gave_up is True
    assert model.forwards == 0          # nothing ran on a model that never loaded
    assert model.state == "failed"


def test_a_model_that_loads_but_cannot_run_is_not_ready():
    """Loading can succeed and the first forward pass still fail (out of memory, incompatible kernels). Better to crash now than on a customer photo."""
    model = FakeYolo(forward_failures=1)
    with pytest.raises(InferenceError):
        model.preload()
    assert model.state == "failed"


def test_preload_on_an_already_loaded_model_does_not_load_again():
    model = FakeYolo()
    model.preload()
    model.events.clear()
    report = model.preload()
    assert "load" not in model.events and report["attempts"] == 1


def test_mock_and_http_inference_have_nothing_to_load():
    assert MockInference().preload() == {}
    from app.adapters.inference import HttpInference
    assert HttpInference("http://inference:8000").preload() == {}


def test_the_throwaway_forward_pass_uses_a_blank_image_and_both_models(monkeypatch):
    """_warm_forward must reach the sticker model too: it is only used when a windshield is found, so a window-only warm-up would leave its
    first-call cost for the first real photo."""
    np = pytest.importorskip("numpy")
    model = YoloInference("yolov5", "window.pt", "sticker.pt")
    seen = []
    model._window_model, model._sticker_model = "window", "sticker"
    monkeypatch.setattr(YoloInference, "_detect", staticmethod(lambda m, image, size: seen.append((m, image.shape, size)) or []))
    model._warm_forward()
    assert [m for m, _, _ in seen] == ["window", "sticker"]
    assert all(isinstance(shape, tuple) and len(shape) == 3 for _, shape, _ in seen)
    assert np is not None
