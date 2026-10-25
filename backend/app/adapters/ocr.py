"""License-plate OCR port and its adapters (docs/adr/0015).

The API only depends on `read(image_bytes) -> PlateReading`. It is called for exactly one photo per
violation: the one that confirmed it (api/device.py). Photos of permitted cars are never read, so no
plate of a lawfully parked car is stored.

- EasyKoreanLpOcr: plate detector (YOLOv5 `lp_det.pt`) + EasyOCR with the custom Korean recognizer
  from https://github.com/gyupro/EasyKoreanLpDetector. Loaded in-process on first use, like the
  windshield/sticker models. MVP simplification of the upstream pipeline: the car-detection stage is
  skipped and the largest plate in the photo is read (the violating car is the one filling the frame).
- MockOcr: fixed answer, for tests.
- NoOcr: OCR_ENABLED is off (default). The caller checks `.enabled` and skips the step.

A failure here must never undo the violation or its notification: the caller records a history line.
"""
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class OcrError(Exception):
    """The models could not be loaded or failed while running."""


@dataclass
class PlateReading:
    plate_number: str | None      # None: no plate found or the reading was below OCR_MIN_CONF
    confidence: float | None      # recognizer confidence of the best reading (also when rejected)
    elapsed_ms: int = 0
    backend: str = "unknown"
    plate_box: list | None = None  # [x1, y1, x2, y2] of the plate that was read


class NoOcr:
    name = "disabled"
    enabled = False

    def read(self, image_bytes: bytes) -> PlateReading:
        raise OcrError("OCR is disabled (OCR_ENABLED=false)")


class MockOcr:
    name = "mock"
    enabled = True

    def __init__(self, plate_number: str | None = "12가3456", confidence: float | None = 0.97):
        self.plate_number = plate_number
        self.confidence = confidence

    def read(self, image_bytes: bytes) -> PlateReading:
        if not image_bytes:
            raise OcrError("empty image")
        return PlateReading(self.plate_number, self.confidence, backend=self.name)


class EasyKoreanLpOcr:
    name = "easykoreanlp"
    enabled = True

    PLATE_SIZE = (224, 128)  # what the recognizer was validated with (upstream server.py)

    def __init__(self, yolov5_dir, plate_model_path, recog_dir, min_conf=0.5, image_size=640):
        self.yolov5_dir = str(yolov5_dir)
        self.plate_model_path = str(plate_model_path)
        self.recog_dir = Path(recog_dir)
        self.min_conf = float(min_conf)
        self.image_size = int(image_size)
        self._lock = threading.Lock()
        self._plate_model = None
        self._reader = None

    def _ensure_loaded(self) -> None:
        if self._reader is not None:
            return
        with self._lock:
            if self._reader is not None:
                return
            try:
                import pathlib

                import easyocr
                import torch

                if sys.platform == "win32":  # checkpoints saved on Linux contain PosixPath objects
                    pathlib.PosixPath = pathlib.WindowsPath

                started = time.time()
                plate_model = torch.hub.load(self.yolov5_dir, "custom", path=self.plate_model_path,
                                             source="local", verbose=False)
                # detector=False: only the recognizer is used (we already have the plate box), so the
                # CRAFT text-detector weights are neither loaded nor downloaded.
                reader = easyocr.Reader(
                    ["en"], gpu=False, detector=False, recog_network="best_acc",
                    user_network_directory=str(self.recog_dir / "user_network"),
                    model_storage_directory=str(self.recog_dir / "models"),
                    download_enabled=False, verbose=False,
                )
                self._plate_model = plate_model
                self._reader = reader
                log.info("plate OCR models loaded in %.1fs", time.time() - started)
            except Exception as exc:  # missing easyocr, missing files, incompatible checkpoint ...
                raise OcrError(f"OCR model load failed: {exc}") from exc

    def read(self, image_bytes: bytes) -> PlateReading:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise OcrError("cannot decode image")
        self._ensure_loaded()
        started = time.time()
        try:
            with self._lock:  # the hub model and the reader are not safe for concurrent calls
                results = self._plate_model(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), size=self.image_size)
                boxes = results.pandas().xyxy[0]
                if boxes.empty:
                    return PlateReading(None, None, int((time.time() - started) * 1000), self.name)
                boxes = boxes.assign(area=(boxes.xmax - boxes.xmin) * (boxes.ymax - boxes.ymin))
                best = boxes.sort_values("area", ascending=False).iloc[0]
                x1, y1, x2, y2 = (max(0, int(best.xmin)), max(0, int(best.ymin)), int(best.xmax), int(best.ymax))
                crop = image[y1:y2, x1:x2]
                if not crop.size:
                    return PlateReading(None, None, int((time.time() - started) * 1000), self.name, [x1, y1, x2, y2])
                gray = cv2.cvtColor(cv2.resize(crop, self.PLATE_SIZE), cv2.COLOR_BGR2GRAY)
                readings = self._reader.recognize(gray)  # [(box, text, confidence), ...]
        except Exception as exc:
            raise OcrError(f"OCR failed: {exc}") from exc

        elapsed = int((time.time() - started) * 1000)
        if not readings:
            return PlateReading(None, None, elapsed, self.name, [x1, y1, x2, y2])
        _, text, confidence = max(readings, key=lambda r: r[2])
        text = "".join(str(text).split())
        confidence = round(float(confidence), 4)
        if not text or confidence < self.min_conf:
            log.info("plate reading rejected: %r conf=%.2f < %.2f", text, confidence, self.min_conf)
            return PlateReading(None, confidence, elapsed, self.name, [x1, y1, x2, y2])
        return PlateReading(text, confidence, elapsed, self.name, [x1, y1, x2, y2])


def build_ocr(config) -> object:
    if not config.get("OCR_ENABLED", False):
        return NoOcr()
    if config.get("OCR_BACKEND", "easykoreanlp") == "mock":
        return MockOcr()
    return EasyKoreanLpOcr(
        yolov5_dir=config["YOLOV5_DIR"],
        plate_model_path=config["OCR_PLATE_MODEL_PATH"],
        recog_dir=config["OCR_RECOG_DIR"],
        min_conf=config["OCR_MIN_CONF"],
        image_size=config["INFERENCE_IMAGE_SIZE"],
    )
