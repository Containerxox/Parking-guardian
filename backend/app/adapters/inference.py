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
from dataclasses import dataclass, field

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


class MockInference:
    """Decides from the file name: contains 'permitted' or 'novehicle', otherwise the default verdict."""

    name = "mock"

    def __init__(self, default_verdict: str = VIOLATION):
        self.default_verdict = default_verdict

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
    """Two-stage detection, same idea as the legacy process_model.py:

    1. Find windshields in the full image, keep only the windshield class, pick the largest box.
    2. Crop that box and look for a disabled-parking sticker inside the crop.
    No windshield -> no vehicle. Sticker found -> permitted. Otherwise -> violation.
    """

    name = "yolo"

    def __init__(self, yolov5_dir, window_model_path, sticker_model_path, window_class_name="car_window",
                 window_conf=0.25, sticker_conf=0.25, image_size=640):
        self.yolov5_dir = str(yolov5_dir)
        self.window_model_path = str(window_model_path)
        self.sticker_model_path = str(sticker_model_path)
        # Accept several names ("windshield,car_window") so the windshield model can be swapped
        # for another training run without touching code.
        self.window_class_names = {name.strip() for name in str(window_class_name).split(",") if name.strip()}
        self.window_conf = window_conf
        self.sticker_conf = sticker_conf
        self.image_size = image_size
        self._lock = threading.Lock()
        self._window_model = None
        self._sticker_model = None

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
                log.info("YOLOv5 models loaded in %.1fs", time.time() - started)
            except InferenceError:
                raise
            except Exception as exc:  # missing torch, missing files, incompatible checkpoint ...
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
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise InvalidImageError("cannot decode image")

        self._ensure_loaded()
        started = time.time()
        try:
            with self._lock:  # the hub models are not safe for concurrent calls
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
                    for det in self._detect(self._sticker_model, crop, self.image_size):
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
    )
