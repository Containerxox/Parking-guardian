"""Plate OCR runs once, on the photo that confirmed a violation, and never gets in the way (ADR 0015)."""
from app.adapters.ocr import MockOcr, NoOcr, OcrError, PlateReading, build_ocr

from .conftest import API

GANGNAM = "RPI-A-GANGNAM-01"


def _detail(client, auth, violation_id):
    return client.get(f"{API}/violations/{violation_id}", headers=auth("kim@a.com")).get_json()


def test_ocr_is_off_by_default_and_violations_have_no_plate(app, client, auth, upload):
    assert isinstance(app.extensions["pg_ocr"], NoOcr)
    violation_id = upload(GANGNAM).get_json()["violation_id"]
    detail = _detail(client, auth, violation_id)
    assert detail["plate_number"] is None and detail["plate_confidence"] is None


def test_confirmed_violation_gets_a_plate_number(app, client, auth, upload):
    app.extensions["pg_ocr"] = MockOcr("12가3456", 0.97)
    response = upload(GANGNAM)
    assert response.status_code == 201 and response.get_json()["plate_number"] == "12가3456"
    detail = _detail(client, auth, response.get_json()["violation_id"])
    assert detail["plate_number"] == "12가3456" and detail["plate_confidence"] == 0.97
    assert [h["action"] for h in detail["history"]] == ["DETECTED", "NOTIFIED"]


def test_ocr_runs_only_for_the_confirming_photo(app, client, auth, upload):
    class CountingOcr(MockOcr):
        calls = 0

        def read(self, image_bytes):
            CountingOcr.calls += 1
            return super().read(image_bytes)

    app.extensions["pg_ocr"] = CountingOcr()
    upload(GANGNAM)                       # confirms (VIOLATION_CONFIRM_COUNT=1 in tests)
    for _ in range(4):
        upload(GANGNAM)                   # violation_ongoing: same car, no OCR
    upload(GANGNAM, "permitted.jpg")      # permitted cars are never read
    assert CountingOcr.calls == 1


def test_ocr_failure_keeps_the_violation_and_the_notification(app, client, auth, upload):
    class BrokenOcr(MockOcr):
        def read(self, image_bytes):
            raise OcrError("model missing")

    app.extensions["pg_ocr"] = BrokenOcr()
    response = upload(GANGNAM)
    assert response.status_code == 201 and "plate_number" not in response.get_json()
    detail = _detail(client, auth, response.get_json()["violation_id"])
    assert detail["plate_number"] is None and detail["status"] == "NOTIFIED"
    assert [h["action"] for h in detail["history"]] == ["DETECTED", "OCR_FAILED", "NOTIFIED"]
    unread = client.get(f"{API}/notifications?unread=true", headers=auth("kim@a.com")).get_json()["unread_count"]
    assert unread == 1


def test_unreadable_plate_is_recorded_as_a_history_line(app, client, auth, upload):
    app.extensions["pg_ocr"] = MockOcr(None, 0.31)   # a plate was seen but the reading was too uncertain
    detail = _detail(client, auth, upload(GANGNAM).get_json()["violation_id"])
    assert detail["plate_number"] is None and detail["plate_confidence"] == 0.31
    assert "OCR_FAILED" in [h["action"] for h in detail["history"]]


def test_build_ocr_follows_the_settings():
    assert isinstance(build_ocr({"OCR_ENABLED": False}), NoOcr)
    assert isinstance(build_ocr({"OCR_ENABLED": True, "OCR_BACKEND": "mock"}), MockOcr)
    real = build_ocr({"OCR_ENABLED": True, "OCR_BACKEND": "easykoreanlp", "YOLOV5_DIR": "x",
                      "OCR_PLATE_MODEL_PATH": "p", "OCR_RECOG_DIR": "r", "OCR_MIN_CONF": 0.5,
                      "INFERENCE_IMAGE_SIZE": 640})
    assert real.name == "easykoreanlp" and real.enabled and real.min_conf == 0.5
    assert PlateReading(None, None).plate_number is None
