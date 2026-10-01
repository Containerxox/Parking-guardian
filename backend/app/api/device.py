"""Endpoints called by detection devices (Raspberry Pi + camera).

Flow of POST /device/detections:
  1. Authenticate the machine by API key. Its parking lot and company come from the database,
     never from the request, so a device cannot write into another tenant.
  2. Run inference on the photo.
  3. Hand the verdict to the episode service (services/detections.py). A device uploads every
     minute, so most photos only update an existing violation or a counter.
  4. Only when a new violation was created: save the image, then publish the notification event.
     Each of these may fail without undoing the violation (partial failure is recorded).
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request

from .. import adapters
from ..adapters.inference import InferenceError, InvalidImageError
from ..adapters.storage import build_image_key, sniff_image_type
from ..auth import require_device, touch_heartbeat
from ..errors import ApiError, bad_request
from ..extensions import db
from ..models import HistoryAction, ImageStatus, Violation, iso, utcnow
from ..services import detections
from ..services import violations as violation_service

log = logging.getLogger(__name__)
bp = Blueprint("device", __name__)


def _parse_detected_at(raw: str | None) -> datetime:
    """Accept ISO 8601 or 'YYYY-MM-DD HH:MM:SS'. Values without an offset use DEVICE_NAIVE_TZ."""
    if not raw:
        return utcnow()
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise bad_request("detected_at 형식이 올바르지 않습니다. 예: 2026-10-01T12:34:56+09:00")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(current_app.config["DEVICE_NAIVE_TZ"]))
    return value.astimezone(timezone.utc)


@bp.post("/device/heartbeat")
@require_device
def heartbeat():
    touch_heartbeat(g.machine)
    db.session.commit()
    return jsonify({"ok": True, "server_time": iso(utcnow())})


@bp.post("/device/detections")
@require_device
def create_detection():
    machine = g.machine
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise bad_request("file 필드에 이미지를 첨부해 주세요.")
    image_bytes = upload.read()
    if not image_bytes:
        raise bad_request("빈 파일입니다.")
    detected_at = _parse_detected_at(request.form.get("detected_at"))

    # 1. Inference
    try:
        result = adapters.inference().analyze(image_bytes, upload.filename)
    except InvalidImageError:
        raise bad_request("이미지 파일을 읽을 수 없습니다.")
    except InferenceError:
        log.exception("inference failed (machine=%s)", machine.id)
        raise ApiError(503, "inference_unavailable", "AI 추론을 수행할 수 없습니다. 잠시 후 다시 시도해 주세요.")

    # 2. Episode logic: is this a new violation, the same vehicle again, or nothing?
    outcome = detections.record_verdict(machine.id, result.verdict, detected_at, request.form.get("zone_name"))
    body = {"result": outcome.result, "inference": result.to_dict()}
    if outcome.violation is not None:
        body["violation_id"] = outcome.violation.id
    if outcome.episode_ended:
        body["episode_ended"] = True
    if not outcome.created:
        return jsonify(body)

    violation = outcome.violation
    violation_id = violation.id

    # 3. Image (only for the photo that confirmed the violation)
    try:
        extension, _ = sniff_image_type(image_bytes)
        key = build_image_key(violation.company_id, violation.parking_lot_id, violation.id,
                              violation.detected_at, extension)
        adapters.storage().save(key, image_bytes)
        violation.image_key = key
        violation.image_status = ImageStatus.UPLOADED
        violation.image_expires_at = utcnow() + timedelta(days=current_app.config["IMAGE_RETENTION_DAYS"])
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("image upload failed (violation=%s)", violation_id)
        violation = db.session.get(Violation, violation_id)
        violation.image_status = ImageStatus.FAILED
        violation_service.add_history(violation_id, HistoryAction.IMAGE_UPLOAD_FAILED,
                                      "차량 사진 저장에 실패했습니다. 사건 기록은 유지됩니다.")
        db.session.commit()

    # 4. Notification event, once per episode. A failure here must never fail the detection itself.
    try:
        adapters.publisher().publish_violation_detected(violation_id)
    except Exception:
        db.session.rollback()
        log.exception("notification publish failed (violation=%s)", violation_id)
        violation_service.add_history(violation_id, HistoryAction.NOTIFY_FAILED,
                                      "알림 전송에 실패했습니다. 사건 기록은 유지됩니다.")
        db.session.commit()

    return jsonify(body), 201
