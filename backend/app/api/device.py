"""Endpoints called by detection devices (Raspberry Pi + camera).

Flow of POST /device/detections:
  1. Authenticate the machine by API key. Its parking lot and company come from the database,
     never from the request, so a device cannot write into another tenant.
  2. Run inference on the photo.
  3. Hand the verdict to the episode service (services/detections.py). An interval-mode device
     uploads every minute, so most photos only update an existing violation or a counter. An
     event-mode device tags each photo with why it was sent (`event`: arrived / confirm / refresh /
     left); the verdict still comes from the photo.
  4. Only when a new violation was created: save the image, then publish the notification event.
     Each of these may fail without undoing the violation (partial failure is recorded as a history
     line). Licence plate OCR used to run between the two steps; it was removed (ADR 0017).
"""
import logging
import uuid
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
from ..services import commands as device_commands
from ..services import detections, ingest
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


def _parse_event(raw: str | None) -> str | None:
    """Optional `event` form field of an event-mode device. Absent means an interval-mode photo."""
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value not in detections.EVENTS:
        raise bad_request("event 값은 arrived, confirm, refresh, left 중 하나여야 합니다.")
    return value


def _parse_uuid(raw: str | None, field: str, required: bool = True) -> uuid.UUID | None:
    if raw is None or not raw.strip():
        if required:
            raise bad_request(f"{field} 가 필요합니다.")
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise bad_request(f"{field} 는 UUID 여야 합니다.")


def _parse_seq(raw: str | None) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        raise bad_request("seq 는 1 이상의 정수여야 합니다.")
    if value < 1:
        raise bad_request("seq 는 1 이상의 정수여야 합니다.")
    return value


@bp.post("/device/detection-jobs")
@require_device
def create_detection_job():
    """Phase 2 asynchronous ingest (ADR 0019): store the photo, record a job, queue it, answer 202.
    The verdict comes later from a worker. 200 = this event_id was already accepted (same job).
    Off (404) unless a queue is configured; POST /device/detections (Phase 1, synchronous) is untouched."""
    if adapters.queue() is None:
        raise ApiError(404, "async_ingest_disabled", "비동기 접수가 켜져 있지 않습니다(QUEUE_BACKEND=none).")
    received_at = utcnow()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise bad_request("file 필드에 이미지를 첨부해 주세요.")
    image_bytes = upload.read()
    if not image_bytes:
        raise bad_request("빈 파일입니다.")
    event = (request.form.get("event") or "").strip().lower()
    if event not in detections.EVENTS:
        raise bad_request("event 값은 arrived, confirm, refresh, left 중 하나여야 합니다.")
    raw_detected_at = request.form.get("detected_at")
    photo = ingest.PhotoEvent(
        machine_id=g.machine.id, event=event,
        event_id=_parse_uuid(request.form.get("event_id"), "event_id"),
        session_id=_parse_uuid(request.form.get("session_id"), "session_id"),
        seq=_parse_seq(request.form.get("seq")),
        detected_at=_parse_detected_at(raw_detected_at) if raw_detected_at else None,
        command_id=_parse_uuid(request.form.get("command_id"), "command_id", required=False))

    try:
        result = ingest.ingest_photo(photo, image_bytes, received_at)
    except ingest.IngestError as exc:
        raise ApiError(exc.status, exc.code, exc.message)
    body = {"job_id": str(result.job_id), "event_id": str(result.event_id), "status": result.status,
            "duplicate": not result.created, "server_time": iso(utcnow())}
    if result.reason:   # duplicate of the COMMAND: a different event_id for a command that already has its confirm job
        body.update(reason=result.reason, command_id=str(result.command_id),
                    existing_event_id=str(result.existing_event_id))
    return jsonify(body), (202 if result.created else 200)


@bp.post("/device/heartbeat")
@require_device
def heartbeat():
    """Phase 1: no body -> {ok, server_time} (+ the additive fields below, empty commands).
    Phase 2 (design 6.10): optional JSON {session_id, occupied, last_seq, acked_commands}; the response carries the
    confirm commands of that session while it is active, repeated until fulfilled (services/commands.py)."""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    raw_session = body.get("session_id")
    if raw_session is not None and not isinstance(raw_session, str):
        raise bad_request("session_id 는 UUID 문자열이어야 합니다.")
    session_id = _parse_uuid(raw_session, "session_id", required=False)
    raw_acked = body.get("acked_commands") or []
    if not isinstance(raw_acked, list) or not all(isinstance(v, str) for v in raw_acked):
        raise bad_request("acked_commands 는 UUID 문자열 목록이어야 합니다.")
    acked = [_parse_uuid(v, "acked_commands") for v in raw_acked]

    now = utcnow()
    touch_heartbeat(g.machine)
    commands, violation_active = device_commands.for_heartbeat(g.machine.id, session_id, acked, now)
    db.session.commit()
    return jsonify({"ok": True, "server_time": iso(now), "violation_active": violation_active,
                    "commands": commands})


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
    event = _parse_event(request.form.get("event"))

    # 1. Inference
    try:
        result = adapters.inference().analyze(image_bytes, upload.filename)
    except InvalidImageError:
        raise bad_request("이미지 파일을 읽을 수 없습니다.")
    except InferenceError:
        log.exception("inference failed (machine=%s)", machine.id)
        raise ApiError(503, "inference_unavailable", "AI 추론을 수행할 수 없습니다. 잠시 후 다시 시도해 주세요.")

    # 2. Episode logic: is this a new violation, the same vehicle again, or nothing?
    outcome = detections.record_verdict(machine.id, result.verdict, detected_at, request.form.get("zone_name"),
                                        event=event)
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
