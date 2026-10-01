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
    VEHICLE_LEFT = "VEHICLE_LEFT"
    SIGNAL_LOST = "SIGNAL_LOST"


class EndReason:
    """Why an episode ended (violations.end_reason)."""

    VEHICLE_LEFT = "VEHICLE_LEFT"          # the space was seen empty several times in a row
    STICKER_DETECTED = "STICKER_DETECTED"  # a sticker was seen in the same space: the first verdict may be wrong
    SIGNAL_LOST = "SIGNAL_LOST"            # photos stopped arriving for too long


def in_list(column: str, values) -> str:
    """Build a portable CHECK expression: column IN ('A', 'B')."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"
