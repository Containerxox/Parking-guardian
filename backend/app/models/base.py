"""Time helpers and shared constants for models."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes. Everything stored is UTC, so attach the zone when missing."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class Role:
    SUPER_ADMIN = "SUPER_ADMIN"
    COMPANY_ADMIN = "COMPANY_ADMIN"
    ALL = (SUPER_ADMIN, COMPANY_ADMIN)


class Status:
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ALL = (ACTIVE, INACTIVE)


class ViolationStatus:
    DETECTED = "DETECTED"
    NOTIFIED = "NOTIFIED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    ALL = (DETECTED, NOTIFIED, IN_PROGRESS, RESOLVED)
    OPEN = (DETECTED, NOTIFIED, IN_PROGRESS)


class ImageStatus:
    PENDING = "PENDING"
    UPLOADED = "UPLOADED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    ALL = (PENDING, UPLOADED, FAILED, EXPIRED)


class NotificationStatus:
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    READ = "READ"
    ALL = (PENDING, SENT, FAILED, READ)


class Channel:
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    ALL = (IN_APP, EMAIL)


class HistoryAction:
    DETECTED = "DETECTED"
    NOTIFIED = "NOTIFIED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    REOPENED = "REOPENED"
    IMAGE_UPLOAD_FAILED = "IMAGE_UPLOAD_FAILED"
    NOTIFY_FAILED = "NOTIFY_FAILED"
    OCR_FAILED = "OCR_FAILED"
    VEHICLE_LEFT = "VEHICLE_LEFT"
    SIGNAL_LOST = "SIGNAL_LOST"


class EndReason:
    """Why an episode ended (violations.end_reason)."""

    VEHICLE_LEFT = "VEHICLE_LEFT"          # the space was seen empty several times in a row
    STICKER_DETECTED = "STICKER_DETECTED"  # a sticker was seen in the same space: the first verdict may be wrong
    SIGNAL_LOST = "SIGNAL_LOST"            # photos stopped arriving for too long


# --- Phase 2 asynchronous judgement (ADR 0019, design doc 6.4-6.10). Lowercase values as in the design. ---

class DeviceEvent:
    """Why an event-mode device sent a photo. Same values as services.detections.EVENTS."""

    ARRIVED = "arrived"
    CONFIRM = "confirm"
    REFRESH = "refresh"
    LEFT = "left"
    ALL = (ARRIVED, CONFIRM, REFRESH, LEFT)


class Verdict:
    """Inference verdicts. Same values as adapters.inference (not imported: models stay free of the adapter)."""

    VIOLATION = "violation"
    PERMITTED = "permitted"
    NO_VEHICLE = "no_vehicle"
    ALL = (VIOLATION, PERMITTED, NO_VEHICLE)


class JobStatus:
    """detection_jobs.status (design 6.7 state diagram)."""

    PENDING_PUBLISH = "pending_publish"  # in the database, not yet confirmed sent to SQS
    QUEUED = "queued"                    # SQS send confirmed
    PROCESSING = "processing"            # claimed by a worker until lease_until
    DONE = "done"                        # verdict and episode change committed together
    STALE = "stale"                      # verdict recorded, episode state left alone (order guard)
    REJECTED = "rejected"                # permanent error (cannot decode, inbox photo missing)
    DEAD_LETTERED = "dead_lettered"      # exceeded maxReceiveCount, moved to the DLQ
    ALL = (PENDING_PUBLISH, QUEUED, PROCESSING, DONE, STALE, REJECTED, DEAD_LETTERED)


class EffectsStatus:
    """detection_jobs.effects_status: evidence copy + notification after commit. Only for a done job
    that created a violation; NULL otherwise."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    ALL = (PENDING, DONE, FAILED)


class SessionStatus:
    ACTIVE = "active"
    ENDED = "ended"            # left reported
    SUPERSEDED = "superseded"  # a newer session of the same machine started (e.g. device reboot)
    ALL = (ACTIVE, ENDED, SUPERSEDED)


class CommandType:
    CAPTURE_CONFIRM = "capture_confirm"
    ALL = (CAPTURE_CONFIRM,)


class CommandStatus:
    PENDING = "pending"        # created by the worker
    DELIVERED = "delivered"    # carried in a heartbeat response
    FULFILLED = "fulfilled"    # a confirm photo with this command_id was received
    CANCELLED = "cancelled"    # session ended, left, or a new session
    EXPIRED = "expired"        # expires_at passed
    ALL = (PENDING, DELIVERED, FULFILLED, CANCELLED, EXPIRED)
    ACTIVE = (PENDING, DELIVERED)


def in_list(column: str, values) -> str:
    """Build a portable CHECK expression: column IN ('A', 'B')."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"
