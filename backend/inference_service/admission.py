"""Admission control in front of the model: how many photos may run, how many may wait.

    running  photos inside the model right now (max_running, fixed at 1: the model lock stays)
    waiting  photos queued for a free slot (max_waiting, INFERENCE_MAX_WAITING)

One more photo than running + waiting is refused immediately (Busy) instead of queueing without bound. The API
side turns that into a quick 503 / one retry, so a burst costs the caller milliseconds, not a full deadline.
The limit lives in this process's memory only. Two gunicorn workers would each get their own gate (and
their own copy of the model), doubling both the admitted photos and the memory; so the inference service runs
ONE worker and gets more capacity from more Pods (total admitted = Pods x (1 + max_waiting)). The entry point
must keep workers = 1 (design doc 3.5, step 2-6).
The two counters are separate on purpose (and separate gauges in app/metrics.py): "running" saturates at 1
under any load, "waiting" is the one that shows how deep the line is.
"""
import threading
from contextlib import contextmanager

from app import metrics


class Busy(Exception):
    """running and waiting are both full."""


class AdmissionGate:
    def __init__(self, max_waiting: int, max_running: int = 1):
        self.max_waiting = max(0, int(max_waiting))
        self.max_running = max_running
        self.running = 0
        self.waiting = 0
        self._free = threading.Condition()

    @contextmanager
    def slot(self):
        """Hold a running slot for the `with` body. Raises Busy at once when there is no room even to wait."""
        self._enter()
        try:
            yield
        finally:
            self._leave()

    def _enter(self) -> None:
        with self._free:
            if self.running >= self.max_running:
                if self.waiting >= self.max_waiting:
                    metrics.INFERENCE_REJECTED.inc()
                    raise Busy()
                self.waiting += 1
                metrics.INFERENCE_WAITING.set(self.waiting)
                try:
                    while self.running >= self.max_running:
                        self._free.wait()
                finally:
                    self.waiting -= 1
                    metrics.INFERENCE_WAITING.set(self.waiting)
            self.running += 1
            metrics.INFERENCE_RUNNING.set(self.running)

    def _leave(self) -> None:
        with self._free:
            self.running -= 1
            metrics.INFERENCE_RUNNING.set(self.running)
            self._free.notify()
