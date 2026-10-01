"""Decide who is notified about a violation and deliver the notifications.

Recipients = ACTIVE COMPANY_ADMINs assigned to the violation's parking lot (admin_parking_lots).
Viewing rights are company-wide, but only assigned admins are notified.

notify_violation() is idempotent: the unique key (violation, admin, channel) means a repeated
call (for example a queue redelivery) does not create or send duplicates.
"""
import logging

from sqlalchemy import select

from .. import adapters
from ..extensions import db
from ..models import (Admin, AdminParkingLot, Channel, HistoryAction, Notification, NotificationStatus, Role,
                      Status, Violation, ViolationStatus, iso, utcnow)
from . import violations as violation_service

log = logging.getLogger(__name__)


def recipients_for(violation: Violation) -> list[Admin]:
    return list(db.session.scalars(
        select(Admin)
        .join(AdminParkingLot, AdminParkingLot.admin_id == Admin.id)
        .where(
            AdminParkingLot.parking_lot_id == violation.parking_lot_id,
            Admin.company_id == violation.company_id,
            Admin.role == Role.COMPANY_ADMIN,
            Admin.status == Status.ACTIVE,
        )
        .order_by(Admin.id)
    ))


def _deliver(notification: Notification, admin: Admin, violation: Violation) -> None:
    notification.attempt_count += 1
    try:
        if notification.channel == Channel.EMAIL:
            lot = violation.parking_lot
            zone = violation.parking_zone.zone_name if violation.parking_zone else "구역 미지정"
            adapters.email_sender().send(
                to=admin.email,
                subject=f"[Parking Guardian] {lot.name} 불법주차 의심 차량 탐지",
                body=(f"{admin.name}님, 담당 주차장에서 불법주차 의심 차량이 탐지되었습니다.\n"
                      f"주차장: {lot.name}\n구역: {zone}\n탐지 시각(UTC): {iso(violation.detected_at)}\n"
                      f"앱에서 확인 후 현장 조치를 진행해 주세요."),
            )
        # IN_APP needs no delivery step: the row itself is what the app shows.
        notification.status = NotificationStatus.SENT
        notification.sent_at = utcnow()
        notification.last_error = None
    except Exception as exc:  # one failing channel must not stop the others
        log.exception("notification delivery failed (id=%s)", notification.id)
        notification.status = NotificationStatus.FAILED
        notification.last_error = str(exc)[:500]


def notify_violation(violation_id: int) -> int:
    """Create and deliver notifications for one violation. Returns the number of recipients."""
    violation = db.session.get(Violation, violation_id)
    if violation is None:
        return 0

    admins = recipients_for(violation)
    if not admins:
        violation_service.add_history(
            violation.id, HistoryAction.NOTIFY_FAILED,
            "이 주차장에 배정된 담당 관리자가 없어 알림을 보내지 못했습니다.",
        )
        db.session.commit()
        return 0

    existing = {
        (n.admin_id, n.channel): n
        for n in db.session.scalars(select(Notification).where(Notification.violation_id == violation.id))
    }
    for admin in admins:
        for channel in Channel.ALL:
            notification = existing.get((admin.id, channel))
            if notification is None:
                notification = Notification(violation_id=violation.id, admin_id=admin.id, channel=channel,
                                            status=NotificationStatus.PENDING, attempt_count=0)
                db.session.add(notification)
                db.session.flush()
            if notification.status in (NotificationStatus.PENDING, NotificationStatus.FAILED):
                _deliver(notification, admin, violation)
    db.session.commit()

    if violation.status == ViolationStatus.DETECTED:
        violation_service.mark_notified(violation, len(admins))
    return len(admins)
