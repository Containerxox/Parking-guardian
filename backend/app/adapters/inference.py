"""Inference port and its adapters.

The API only depends on `analyze(image_bytes, filename) -> InferenceResult`.
- YoloInference: the two YOLOv5 models, loaded in-process (current phase).
- MockInference: no model needed. Used by tests and when torch is not installed.
A later phase replaces YoloInference with an HTTP client to a separate inference service.
"""
import logging
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from .. import metrics

log = logging.getLogger(__name__)

NO_VEHICLE = "no_vehicle"
PERMITTED = "permitted"
VIOLATION = "violation"


class InvalidImageError(Exception):
    """The uploaded bytes could not be decoded as an image."""


class InferenceError(Exception):
    """The model could not be loaded or failed while running."""


@dataclass
class InferenceResult:
    has_window: bool
    stickers: list = field(default_factory=list)
    window: dict | None = None
    elapsed_ms: int = 0
    backend: str = "unknown"

    @property
    def verdict(self) -> str:
        if not self.has_window:
            return NO_VEHICLE
        return PERMITTED if self.stickers else VIOLATION

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "verdict": self.verdict,
            "window": self.window,
            "sticker_count": len(self.stickers),
            "stickers": self.stickers,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, body) -> "InferenceResult":
        """Parse what `to_dict()` produced (the inference service's JSON response).

        `verdict` and `sticker_count` are derived, so they are only used to check that the body agrees with
        itself. Anything malformed becomes an InferenceError, which device.py already answers with 503.
        """
        try:
            if not isinstance(body, dict):
                raise ValueError("body is not an object")
            verdict = body["verdict"]
            if verdict not in (NO_VEHICLE, PERMITTED, VIOLATION):
                raise ValueError(f"unknown verdict {verdict!r}")
            window = body.get("window")
            stickers = body.get("stickers", [])
            if not isinstance(stickers, list):
                raise ValueError("stickers is not a list")
            elapsed_ms = body.get("elapsed_ms", 0)
            if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int):
                raise ValueError("elapsed_ms is not an integer")
            result = cls(has_window=window is not None, stickers=stickers, window=window,
                         elapsed_ms=elapsed_ms, backend=str(body.get("backend", "unknown")))
            if result.verdict != verdict:
                raise ValueError(f"verdict {verdict!r} does not match window/stickers ({result.verdict!r})")
            return result
        except (KeyError, ValueError, TypeError) as exc:
            raise InferenceError(f"malformed inference response: {exc}") from exc


class MockInference:
    """Decides from the file name: contains 'permitted' or 'novehicle', otherwise the default verdict."""

    name = "mock"
    state = "ready"  # nothing to load

    def __init__(self, default_verdict: str = VIOLATION):
        self.default_verdict = default_verdict

    def warm_up(self) -> None:
        """No model to load."""

    def analyze(self, image_bytes: bytes, filename: str = "") -> InferenceResult:
        if not image_bytes:
            raise InvalidImageError("empty image")
        lowered = (filename or "").lower()
        verdict = self.default_verdict
        if "permitted" in lowered:
            verdict = PERMITTED
        elif "novehicle" in lowered:
            verdict = NO_VEHICLE
        elif "violation" in lowered:
            verdict = VIOLATION

        window = {"coords": [0, 0, 100, 100], "confidence": 1.0}
        if verdict == NO_VEHICLE:
            return InferenceResult(has_window=False, backend=self.name)
        stickers = [{"coords": [10, 10, 20, 20], "confidence": 1.0}] if verdict == PERMITTED else []
        return InferenceResult(has_window=True, window=window, stickers=stickers, backend=self.name)


class YoloInference:
    """Two-stage detection, same idea as the original capstone process_model.py (git tag legacy-capstone):

    1. Find windshields in the full image, keep only the windshield class, pick the largest box.
    2. Crop that box and look for a disabled-parking sticker inside the crop.
    No windshield -> no vehicle. Sticker found -> permitted. Otherwise -> violation.
    """

    name = "yolo"

    def __init__(self, yolov5_dir, window_model_path, sticker_model_path, window_class_name="car_window",
                 window_conf=0.25, sticker_conf=0.25, image_size=640, load_attempts=3, retry_delay=5.0):
        self.yolov5_dir = str(yolov5_dir)
        self.window_model_path = str(window_model_path)
        self.sticker_model_path = str(sticker_model_path)
        # Accept several names ("windshield,car_window") so the windshield model can be swapped
        # for another training run without touching code.
        self.window_class_names = {name.strip() for name in str(window_class_name).split(",") if name.strip()}
        self.window_conf = window_conf
        self.sticker_conf = sticker_conf
        self.image_size = image_size
        # warm_up() tries to load this many times, waiting retry_delay * attempt seconds in between. After the
        # last failure `gave_up` is True and nothing retries in the background any more.
        self.load_attempts = max(1, int(load_attempts))
        self.retry_delay = retry_delay
        self.gave_up = False
        self._lock = threading.Lock()
        self._window_model = None
        self._sticker_model = None
        # cold: not loaded, will load on the first photo (local runs, CLI).
        # loading: warm_up() is loading in the background; /readyz answers 503 so no traffic arrives yet.
        # ready: both models loaded. failed: the last load attempt raised; the next photo retries.
        self.state = "cold"

    def _set_state(self, state: str) -> None:
        self.state = state
        metrics.MODEL_STATE.state(state)

    def warm_up(self) -> None:
        """Load the models in a background thread (called once per gunicorn worker, see gunicorn.conf.py).

        Without this a freshly scaled-out Pod is Ready while its first photo still pays the 25-35 s
        model load. With it, /readyz stays 503 until the models are in memory, so the ALB and the
        HPA only see the Pod once it can actually answer in ~1 s.
        """
        if self.state != "cold":
            return
        self._set_state("loading")

        def run():
            for attempt in range(1, self.load_attempts + 1):
                try:
                    self._ensure_loaded()
                    return
                except Exception:  # already logged and recorded as state="failed"; the API keeps serving
                    if attempt == self.load_attempts:
                        break
                    time.sleep(self.retry_delay * attempt)
                    self._set_state("loading")
            self.gave_up = True
            log.error("model load failed %d times, giving up", self.load_attempts)

        threading.Thread(target=run, name="inference-warm-up", daemon=True).start()

    # Models are loaded on first use so the API server starts quickly and works without torch
    # until a device actually uploads an image.
    def _ensure_loaded(self):
        if self._window_model is not None:
            return
        with self._lock:
            if self._window_model is not None:
                return
            try:
                import pathlib

                import torch

                # The checkpoints were saved on Linux (Colab) and contain PosixPath objects,
                # which Windows cannot instantiate. Linux containers do not need this.
                if sys.platform == "win32":
                    pathlib.PosixPath = pathlib.WindowsPath

                started = time.time()
                window_model = torch.hub.load(self.yolov5_dir, "custom", path=self.window_model_path,
                                              source="local", verbose=False)
                sticker_model = torch.hub.load(self.yolov5_dir, "custom", path=self.sticker_model_path,
                                               source="local", verbose=False)
                window_model.conf = self.window_conf
                sticker_model.conf = self.sticker_conf
                if not self.window_class_names & set(window_model.names.values()):
                    raise InferenceError(
                        f"window model has none of the classes {sorted(self.window_class_names)}: {window_model.names}")
                self._sticker_model = sticker_model
                self._window_model = window_model
                elapsed = time.time() - started
                metrics.MODEL_LOAD_SECONDS.set(elapsed)
                self._set_state("ready")
                log.info("YOLOv5 models loaded in %.1fs", elapsed)
            except InferenceError:
                self._set_state("failed")
                log.exception("model load failed")
                raise
            except Exception as exc:  # missing torch, missing files, incompatible checkpoint ...
                self._set_state("failed")
                log.exception("model load failed")
                raise InferenceError(f"model load failed: {exc}") from exc

    @staticmethod
    def _detect(model, image_bgr, size):
        import cv2

        results = model(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB), size=size)
        frame = results.pandas().xyxy[0]
        return [
            {
                "name": row["name"],
                "coords": [int(row["xmin"]), int(row["ymin"]), int(row["xmax"]), int(row["ymax"])],
                "confidence": round(float(row["confidence"]), 4),
            }
            for _, row in frame.iterrows()
        ]

    def analyze(self, image_bytes: bytes, filename: str = "") -> InferenceResult:
        """Decode, then run the two models under the lock. Records lock wait, stage times and the outcome."""
        metrics.INFERENCE_IN_FLIGHT.inc()
        try:
            result = self._analyze(image_bytes)
        except InvalidImageError:
            metrics.INFERENCE_REQUESTS.labels("", "invalid_image").inc()
            raise
        except InferenceError:
            metrics.INFERENCE_REQUESTS.labels("", "error").inc()
            raise
        finally:
            metrics.INFERENCE_IN_FLIGHT.dec()
        metrics.INFERENCE_REQUESTS.labels(result.verdict, "ok").inc()
        return result

    def _analyze(self, image_bytes: bytes) -> InferenceResult:
        import cv2
        import numpy as np

        with metrics.INFERENCE_STAGE.labels("decode").time():
            image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise InvalidImageError("cannot decode image")

        self._ensure_loaded()
        started = time.time()
        try:
            queued = time.perf_counter()
            with self._lock:  # the hub models are not safe for concurrent calls
                metrics.INFERENCE_LOCK_WAIT.observe(time.perf_counter() - queued)
                with metrics.INFERENCE_STAGE.labels("window").time():
                    windows = [d for d in self._detect(self._window_model, image, self.image_size)
                               if d["name"] in self.window_class_names]
                if not windows:
                    return InferenceResult(has_window=False, backend=self.name,
                                           elapsed_ms=int((time.time() - started) * 1000))

                def area(det):
                    x1, y1, x2, y2 = det["coords"]
                    return (x2 - x1) * (y2 - y1)

                largest = max(windows, key=area)
                x1, y1, x2, y2 = largest["coords"]
                crop = image[max(0, y1):y2, max(0, x1):x2]
                stickers = []
                if crop.size:
                    with metrics.INFERENCE_STAGE.labels("sticker").time():
                        detections = self._detect(self._sticker_model, crop, self.image_size)
                    for det in detections:
                        sx1, sy1, sx2, sy2 = det["coords"]
                        # crop-relative -> absolute coordinates in the original image
                        stickers.append({"coords": [x1 + sx1, y1 + sy1, x1 + sx2, y1 + sy2],
                                         "confidence": det["confidence"]})
        except Exception as exc:
            raise InferenceError(f"inference failed: {exc}") from exc

        return InferenceResult(
            has_window=True,
            window={"coords": largest["coords"], "confidence": largest["confidence"]},
            stickers=stickers,
            elapsed_ms=int((time.time() - started) * 1000),
            backend=self.name,
        )


class _CallFailed(Exception):
    """Internal: how one analyze() call ended badly. `outcome` is the metric label, `error` what the caller sees."""

    def __init__(self, outcome: str, error: Exception):
        super().__init__(str(error))
        self.outcome = outcome
        self.error = error


class HttpInference:
    """Asks the separate inference service (inference_service/) instead of loading the models in this process.

    Every kind of failure has exactly one treatment (tests/test_inference_http.py has the same table):

      200                              InferenceResult
      400 invalid_image                InvalidImageError, never retried: the same photo would fail again
      503 busy                         retried (the service refused before touching the photo)
      could not connect / connect timeout   retried (nothing reached the service)
      503 model_unavailable            InferenceError, not retried: the service said it cannot judge
      read timeout, connection dropped after sending     InferenceError, not retried: the photo may be in
                                       progress, sending it again would only add load
      anything else (other status, bad JSON, self-contradicting body)    InferenceError, not retried
      bulkhead full                    InferenceError at once, nothing is sent

    Retries: at most `retries` extra attempts, each only if the wait fits in what is left of `deadline` (one
    budget for the whole call, including waits). The wait is the Retry-After of the service, or `retry_backoff`
    when absent/unreadable; a Retry-After above `max_retry_wait` means "do not bother" and ends the call.
    Timeouts per attempt: connect = min(connect_timeout, time left), read = time left. The read timeout bounds
    the wait for the first byte of the answer, which is the whole analysis because the service replies once.

    Bulkhead: at most `max_concurrency` calls at the same time; the next one fails immediately, so a slow
    inference service cannot hold every API thread (the admin API keeps its threads).

    `device.py` is unchanged: InferenceError -> 503 inference_unavailable, InvalidImageError -> 400.
    """

    name = "http"
    state = "remote"  # /readyz must not depend on the inference service (design doc 3.4)

    def __init__(self, base_url, deadline=10.0, connect_timeout=1.0, retries=1, max_concurrency=4,
                 retry_backoff=0.2, max_retry_wait=2.0, session=None, clock=time.monotonic, sleep=time.sleep):
        self.url = str(base_url).rstrip("/") + "/v1/analyze"
        self.deadline = float(deadline)
        self.connect_timeout = float(connect_timeout)
        self.retries = max(0, int(retries))
        self.retry_backoff = float(retry_backoff)
        self.max_retry_wait = float(max_retry_wait)
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self._clock = clock
        self._sleep = sleep
        if session is None:
            import requests
            from requests.adapters import HTTPAdapter

            session = requests.Session()
            adapter = HTTPAdapter(pool_maxsize=max(1, int(max_concurrency)))
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self._session = session

    def warm_up(self) -> None:
        """Nothing to load here; the inference service warms itself up."""

    def analyze(self, image_bytes: bytes, filename: str = "") -> InferenceResult:
        if not self._slots.acquire(blocking=False):
            metrics.AI_CLIENT_REQUESTS.labels("bulkhead_full").inc()
            raise InferenceError("too many calls to the inference service in flight")
        metrics.AI_CLIENT_IN_FLIGHT.inc()
        started = self._clock()
        try:
            try:
                result = self._call(image_bytes, filename, started)
            except _CallFailed as failed:
                metrics.AI_CLIENT_REQUESTS.labels(failed.outcome).inc()
                raise failed.error from failed.__cause__
            metrics.AI_CLIENT_REQUESTS.labels("ok").inc()
            return result
        finally:
            metrics.AI_CLIENT_DURATION.observe(self._clock() - started)
            metrics.AI_CLIENT_IN_FLIGHT.dec()
            self._slots.release()

    def _call(self, image_bytes: bytes, filename: str, started: float) -> InferenceResult:
        import requests

        headers = {"Content-Type": "application/octet-stream", "X-Filename": quote(filename or "", safe=""),
                   "X-Request-Id": uuid.uuid4().hex}  # the same id on every attempt of this call
        give_up_at = started + self.deadline
        attempt = 0
        while True:
            remaining = give_up_at - self._clock()
            if remaining <= 0:
                raise _CallFailed("timeout", InferenceError("inference service deadline exceeded"))
            try:
                response = self._session.post(self.url, data=image_bytes, headers=headers,
                                              timeout=(min(self.connect_timeout, remaining), remaining))
            except requests.exceptions.ConnectTimeout as exc:
                failure, reason, wait = self._connect_failure(exc), "connect_error", self.retry_backoff
            except requests.exceptions.ConnectionError as exc:
                if _may_have_reached_the_service(exc):
                    raise _CallFailed("connection_lost", InferenceError(f"connection lost: {exc}")) from exc
                failure, reason, wait = self._connect_failure(exc), "connect_error", self.retry_backoff
            except requests.exceptions.Timeout as exc:
                raise _CallFailed("timeout", InferenceError("inference service did not answer in time")) from exc
            except requests.exceptions.RequestException as exc:
                raise _CallFailed("bad_response", InferenceError(f"inference request failed: {exc}")) from exc
            else:
                outcome = self._handle(response)  # an InferenceResult, or the seconds to wait after "busy"
                if isinstance(outcome, InferenceResult):
                    return outcome
                failure = _CallFailed("busy", InferenceError("inference service is busy"))
                reason, wait = "busy", outcome
            if attempt >= self.retries or wait > self.max_retry_wait or wait >= give_up_at - self._clock():
                raise failure
            self._sleep(wait)
            attempt += 1
            metrics.AI_CLIENT_RETRIES.labels(reason).inc()

    @staticmethod
    def _connect_failure(exc) -> _CallFailed:
        failure = _CallFailed("connect_error", InferenceError(f"cannot connect to the inference service: {exc}"))
        failure.__cause__ = exc
        return failure

    def _handle(self, response):
        """Turn a response into a result or an exception; for busy, return the seconds to wait before a retry."""
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if status == 200:
            try:
                return InferenceResult.from_dict(body)
            except InferenceError as exc:
                raise _CallFailed("bad_response", exc) from exc
        if status == 400 and error == "invalid_image":
            raise _CallFailed("invalid_image", InvalidImageError("the inference service could not decode the image"))
        if status == 503 and error == "busy":
            return self._retry_after(response)
        if status == 503 and error == "model_unavailable":
            raise _CallFailed("unavailable", InferenceError("inference service model is unavailable"))
        raise _CallFailed("bad_response", InferenceError(f"inference service answered status {status}"))

    def _retry_after(self, response) -> float:
        try:
            return max(0.0, float(response.headers.get("Retry-After")))
        except (TypeError, ValueError):
            return self.retry_backoff


def _may_have_reached_the_service(exc) -> bool:
    """True when requests reports a connection that broke after it was open (the request may have been sent),
    as opposed to one that could never be opened (refused, unreachable)."""
    import urllib3

    cause = exc.args[0] if exc.args else None
    cause = getattr(cause, "reason", cause)
    return isinstance(cause, (urllib3.exceptions.ProtocolError, urllib3.exceptions.ReadTimeoutError))


def build_inference(config) -> object:
    backend = config.get("INFERENCE_BACKEND", "yolo")
    if backend == "mock":
        return MockInference(config.get("MOCK_INFERENCE_VERDICT", VIOLATION))
    return YoloInference(
        yolov5_dir=config["YOLOV5_DIR"],
        window_model_path=config["WINDOW_MODEL_PATH"],
        sticker_model_path=config["STICKER_MODEL_PATH"],
        window_class_name=config["WINDOW_CLASS_NAME"],
        window_conf=config["WINDOW_CONF"],
        sticker_conf=config["STICKER_CONF"],
        image_size=config["INFERENCE_IMAGE_SIZE"],
        load_attempts=config.get("MODEL_LOAD_ATTEMPTS", 3),
        retry_delay=config.get("MODEL_LOAD_RETRY_SECONDS", 5.0),
    )
